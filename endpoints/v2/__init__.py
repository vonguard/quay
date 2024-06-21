import logging
import os.path
from functools import wraps
from urllib.parse import urlencode

from flask import Blueprint, Response, jsonify, make_response, request, url_for
from semantic_version import Spec

import features
from app import app, get_app_url, usermanager
from auth.auth_context import get_authenticated_context
from auth.permissions import (
    AdministerRepositoryPermission,
    ModifyRepositoryPermission,
    ReadRepositoryPermission,
)
from auth.registry_jwt_auth import get_auth_headers, process_registry_jwt_auth
from data.model import QuotaExceededException
from data.readreplica import ReadOnlyModeException
from data.registry_model import registry_model
from endpoints.decorators import anon_allowed, route_show_if
from endpoints.v2.errors import (
    InvalidRequest,
    QuotaExceeded,
    ReadOnlyMode,
    TooManyTagsRequested,
    Unauthorized,
    Unsupported,
    V2RegistryException,
)
from proxy import UpstreamRegistryError
from util.http import abort
from util.metrics.prometheus import timed_blueprint
from util.pagination import decrypt_page_token, encrypt_page_token
from util.registry.dockerver import docker_version

logger = logging.getLogger(__name__)
v2_bp = timed_blueprint(Blueprint("v2", __name__))


@v2_bp.app_errorhandler(V2RegistryException)
def handle_registry_v2_exception(error):
    response = jsonify({"errors": [error.as_dict()]})

    response.status_code = error.http_status_code
    if response.status_code == 401:
        response.headers.extend(get_auth_headers(repository=error.repository, scopes=error.scopes))
    logger.debug("sending response: %s", response.get_data())
    return response


@v2_bp.app_errorhandler(ReadOnlyModeException)
def handle_readonly(ex):
    return _format_error_response(ReadOnlyMode())


@v2_bp.app_errorhandler(UpstreamRegistryError)
def handle_proxy_cache_error(error):
    return _format_error_response(InvalidRequest(message=str(error)))


@v2_bp.app_errorhandler(QuotaExceededException)
def handle_quota_error(error):
    return _format_error_response(QuotaExceeded())


def _format_error_response(error: V2RegistryException) -> Response:
    response = jsonify({"errors": [error.as_dict()]})
    response.status_code = error.http_status_code
    logger.debug("sending response: %s", response.get_data())
    return response


_MAX_RESULTS_PER_PAGE = max(
    app.config.get("V2_PAGINATION_SIZE", 100), 100
)  # minimally server 100 tags per page


def paginate(
    start_id_kwarg_name="start_id",
    limit_kwarg_name="limit",
    callback_kwarg_name="pagination_callback",
):
    """
    Decorates a handler adding a parsed pagination token and a callback to encode a response token.
    """

    def wrapper(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                requested_limit = int(request.args.get("n", _MAX_RESULTS_PER_PAGE))
            except ValueError:
                requested_limit = 0

            limit = max(min(requested_limit, _MAX_RESULTS_PER_PAGE), 1)
            next_page_token = request.args.get("next_page", request.args.get("last", None))

            # Decrypt the next page token, if any.
            start_id = None
            page_info = decrypt_page_token(next_page_token)
            if page_info is not None:
                start_id = page_info.get("start_id", None)

            def callback(results, response):
                if len(results) <= limit:
                    return

                next_page_token = encrypt_page_token({"start_id": max([obj.id for obj in results])})

                link_url = os.path.join(
                    get_app_url(), url_for(request.endpoint, **request.view_args)
                )
                link_param = urlencode({"n": limit, "next_page": next_page_token})
                link = '<%s?%s>; rel="next"' % (link_url, link_param)
                response.headers["Link"] = link

            kwargs[limit_kwarg_name] = limit
            kwargs[start_id_kwarg_name] = start_id
            kwargs[callback_kwarg_name] = callback
            return func(*args, **kwargs)

        return wrapped

    return wrapper


def oci_tag_paginate(
    last_tag_kwarg_name="last_pagination_tag_name",
    limit_kwarg_name="limit",
    callback_kwarg_name="pagination_callback",
):
    """
    Decorates a handler validating sane inputs for pagination and adding a Link header of type rel=next
    according to OCI tag pagination spec via a callback.
    """

    def wrapper(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                requested_limit = request.args.get("n", _MAX_RESULTS_PER_PAGE, type=int)
                last_tag_name = request.args.get("last", None, type=str)
            except ValueError:
                requested_limit = _MAX_RESULTS_PER_PAGE
                last_tag_name = None

            limit = max(min(requested_limit, _MAX_RESULTS_PER_PAGE), 1)

            if last_tag_name is not None:
                last_tag_name = last_tag_name.strip()

            def callback(results, has_more, response):
                if not has_more:
                    return

                link_param = urlencode({"n": limit, "last": results[-1].name})
                link_url = os.path.join(
                    get_app_url(), url_for(request.endpoint, **request.view_args)
                )
                link = '<%s?%s>; rel="next"' % (link_url, link_param)
                response.headers["Link"] = link

            kwargs[limit_kwarg_name] = limit
            kwargs[last_tag_kwarg_name] = last_tag_name
            kwargs[callback_kwarg_name] = callback
            return func(*args, **kwargs)

        return wrapped

    return wrapper


def _require_repo_permission(permission_class, scopes=None, allow_public=False):
    def _require_permission(allow_for_superuser=False, disallow_for_restricted_users=False):
        def wrapper(func):
            @wraps(func)
            def wrapped(namespace_name, repo_name, *args, **kwargs):
                logger.debug(
                    "Checking permission %s for repo: %s/%s",
                    permission_class,
                    namespace_name,
                    repo_name,
                )

                # In the cases where we need to act based on the user to workaround the regular permissions
                # and not just based on the validity of the JWT, we need to hit the database to get the user.
                # This need to be checked before the normal permissions, in order to restrict a user's own namespace
                if features.RESTRICTED_USERS and disallow_for_restricted_users:
                    if allow_public:
                        repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
                        if repository_ref is None or not repository_ref.is_public:
                            raise Unauthorized()

                        if not features.ANONYMOUS_ACCESS:
                            raise Unauthorized()

                        return func(namespace_name, repo_name, *args, **kwargs)

                    context = get_authenticated_context()

                    if (
                        context is not None
                        and context.authed_user is not None
                        and context.authed_user.username == namespace_name
                    ):
                        if usermanager.is_restricted_user(
                            context.authed_user.username,
                            # include_robots=app.config["RESTRICTED_USER_INCLUDE_ROBOTS"],
                        ) and not usermanager.is_superuser(context.authed_user.username):
                            raise Unauthorized(detail="Disallowed for restricted users.")

                permission = permission_class(namespace_name, repo_name)

                if permission.can():
                    return func(namespace_name, repo_name, *args, **kwargs)

                # Superusers' extra permissions
                if features.SUPERUSERS_FULL_ACCESS and allow_for_superuser:
                    context = get_authenticated_context()

                    if context is not None and context.authed_user is not None:
                        if usermanager.is_superuser(context.authed_user.username):
                            return func(namespace_name, repo_name, *args, **kwargs)

                repository = namespace_name + "/" + repo_name
                if allow_public:
                    repository_ref = registry_model.lookup_repository(namespace_name, repo_name)
                    if repository_ref is None or not repository_ref.is_public:
                        raise Unauthorized(repository=repository, scopes=scopes)

                    if repository_ref.kind != "image":
                        msg = (
                            "This repository is for managing %s and not container images."
                            % repository_ref.kind
                        )
                        raise Unsupported(detail=msg)

                    if repository_ref.is_public:
                        if not features.ANONYMOUS_ACCESS:
                            raise Unauthorized(repository=repository, scopes=scopes)

                        return func(namespace_name, repo_name, *args, **kwargs)

                    # Allow public imply a RepositoryRead scope, so we can grant superusers read-only access
                    if features.SUPER_USERS and app.config.get("GLOBAL_READONLY_SUPER_USERS"):
                        context = get_authenticated_context()
                        if (
                            context is not None
                            and context.authed_user is not None
                            and usermanager.is_global_readonly_superuser(context.authed_user)
                        ):
                            return func(namespace_name, repo_name, *args, **kwargs)

                raise Unauthorized(repository=repository, scopes=scopes)

            return wrapped

        return wrapper

    return _require_permission


require_repo_read = _require_repo_permission(
    ReadRepositoryPermission, scopes=["pull"], allow_public=True
)
require_repo_write = _require_repo_permission(ModifyRepositoryPermission, scopes=["pull", "push"])
require_repo_admin = _require_repo_permission(
    AdministerRepositoryPermission, scopes=["pull", "push"]
)


def get_input_stream(flask_request):
    if flask_request.headers.get("transfer-encoding") == "chunked":
        return flask_request.environ["wsgi.input"]
    return flask_request.stream


@v2_bp.route("/")
@route_show_if(features.ADVERTISE_V2)
@process_registry_jwt_auth()
@anon_allowed
def v2_support_enabled():
    docker_ver = docker_version(request.user_agent.string)

    # Check if our version is one of the blacklisted versions, if we can't
    # identify the version (None) we will fail open and assume that it is
    # newer and therefore should not be blacklisted.
    if docker_ver is not None and Spec(app.config["BLACKLIST_V2_SPEC"]).match(docker_ver):
        abort(404)

    response = make_response("true", 200)

    if get_authenticated_context() is None:
        response = make_response("true", 401)

    response.headers.extend(get_auth_headers())
    return response


from endpoints.v2 import blob, catalog, manifest, referrers, tag, v2auth
