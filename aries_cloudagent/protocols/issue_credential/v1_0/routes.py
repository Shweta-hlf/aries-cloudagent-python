"""Credential exchange admin routes."""

import json

from aiohttp import web
from aiohttp_apispec import docs, request_schema, response_schema
from json.decoder import JSONDecodeError
from marshmallow import fields, Schema

from ....connections.models.connection_record import ConnectionRecord
from ....holder.base import BaseHolder
from ....issuer.indy import IssuerRevocationRegistryFullError
from ....messaging.credential_definitions.util import CRED_DEF_TAGS
from ....messaging.valid import (
    INDY_CRED_DEF_ID,
    INDY_DID,
    INDY_REV_REG_ID,
    INDY_SCHEMA_ID,
    INDY_VERSION,
    UUIDFour,
)
from ....storage.error import StorageNotFoundError

from ...problem_report.message import ProblemReport

from .manager import CredentialManager
from .messages.credential_proposal import CredentialProposal
from .messages.inner.credential_preview import (
    CredentialPreview,
    CredentialPreviewSchema,
)
from .models.credential_exchange import (
    V10CredentialExchange,
    V10CredentialExchangeSchema,
)

from ....utils.tracing import trace_event, get_timer


class V10AttributeMimeTypesResultSchema(Schema):
    """Result schema for credential attribute MIME types by credential definition."""


class V10CredentialExchangeListResultSchema(Schema):
    """Result schema for Aries#0036 v1.0 credential exchange query."""

    results = fields.List(
        fields.Nested(V10CredentialExchangeSchema),
        description="Aries#0036 v1.0 credential exchange records",
    )


class V10CredentialStoreRequestSchema(Schema):
    """Request schema for sending a credential store admin message."""

    credential_id = fields.Str(required=False)


class V10CredentialProposalRequestSchemaBase(Schema):
    """Base class for request schema for sending credential proposal admin message."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=True,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    cred_def_id = fields.Str(
        description="Credential definition identifier",
        required=False,
        **INDY_CRED_DEF_ID,
    )
    schema_id = fields.Str(
        description="Schema identifier", required=False, **INDY_SCHEMA_ID
    )
    schema_issuer_did = fields.Str(
        description="Schema issuer DID", required=False, **INDY_DID
    )
    schema_name = fields.Str(
        description="Schema name", required=False, example="preferences"
    )
    schema_version = fields.Str(
        description="Schema version", required=False, **INDY_VERSION
    )
    issuer_did = fields.Str(
        description="Credential issuer DID", required=False, **INDY_DID
    )
    auto_remove = fields.Bool(
        description=(
            "Whether to remove the credential exchange record on completion "
            "(overrides --preserve-exchange-records configuration setting)"
        ),
        required=False
    )
    comment = fields.Str(description="Human-readable comment", required=False)


class V10CredentialProposalRequestOptSchema(V10CredentialProposalRequestSchemaBase):
    """Request schema for sending credential proposal on optional proposal preview."""

    credential_proposal = fields.Nested(CredentialPreviewSchema, required=False)


class V10CredentialProposalRequestMandSchema(V10CredentialProposalRequestSchemaBase):
    """Request schema for sending credential proposal on mandatory proposal preview."""

    credential_proposal = fields.Nested(CredentialPreviewSchema, required=True)


class V10CredentialOfferRequestSchema(Schema):
    """Request schema for sending credential offer admin message."""

    connection_id = fields.UUID(
        description="Connection identifier",
        required=True,
        example=UUIDFour.EXAMPLE,  # typically but not necessarily a UUID4
    )
    cred_def_id = fields.Str(
        description="Credential definition identifier",
        required=True,
        **INDY_CRED_DEF_ID,
    )
    auto_issue = fields.Bool(
        description=(
            "Whether to respond automatically to credential requests, creating "
            "and issuing requested credentials"
        ),
        required=False
    )
    auto_remove = fields.Bool(
        description=(
            "Whether to remove the credential exchange record on completion "
            "(overrides --preserve-exchange-records configuration setting)"
        ),
        required=False,
        default=True,
    )
    comment = fields.Str(description="Human-readable comment", required=False)
    credential_preview = fields.Nested(CredentialPreviewSchema, required=True)


class V10CredentialIssueRequestSchema(Schema):
    """Request schema for sending credential issue admin message."""

    comment = fields.Str(description="Human-readable comment", required=False)
    credential_preview = fields.Nested(CredentialPreviewSchema, required=True)


class V10CredentialProblemReportRequestSchema(Schema):
    """Request schema for sending problem report."""

    explain_ltxt = fields.Str(required=True)


class V10PublishRevocationsResultSchema(Schema):
    """Result schema for revocation publication API call."""

    results = fields.Dict(
        keys=fields.Str(example=INDY_REV_REG_ID["example"]),  # marshmallow 3.0 ignores
        values=fields.List(
            fields.Str(description="Credential revocation identifier", example="23")
        ),
        description="Credential revocation ids published by revocation registry id",
    )


@docs(tags=["issue-credential"], summary="Get attribute MIME types from wallet")
@response_schema(V10AttributeMimeTypesResultSchema(), 200)
async def attribute_mime_types_get(request: web.BaseRequest):
    """
    Request handler for getting credential attribute MIME types.

    Args:
        request: aiohttp request object

    Returns:
        The MIME types response

    """
    context = request.app["request_context"]
    credential_id = request.match_info["credential_id"]
    holder: BaseHolder = await context.inject(BaseHolder)

    return web.json_response(await holder.get_mime_type(credential_id))


@docs(tags=["issue-credential"], summary="Fetch all credential exchange records")
@response_schema(V10CredentialExchangeListResultSchema(), 200)
async def credential_exchange_list(request: web.BaseRequest):
    """
    Request handler for searching connection records.

    Args:
        request: aiohttp request object

    Returns:
        The connection list response

    """
    context = request.app["request_context"]
    tag_filter = {}
    if "thread_id" in request.query and request.query["thread_id"] != "":
        tag_filter["thread_id"] = request.query["thread_id"]
    post_filter = {}
    for param_name in ("connection_id", "role", "state"):
        if param_name in request.query and request.query[param_name] != "":
            post_filter[param_name] = request.query[param_name]
    records = await V10CredentialExchange.query(context, tag_filter, post_filter)
    return web.json_response({"results": [record.serialize() for record in records]})


@docs(tags=["issue-credential"], summary="Fetch a single credential exchange record")
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_retrieve(request: web.BaseRequest):
    """
    Request handler for fetching single connection record.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    context = request.app["request_context"]
    credential_exchange_id = request.match_info["cred_ex_id"]
    try:
        record = await V10CredentialExchange.retrieve_by_id(
            context, credential_exchange_id
        )
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    return web.json_response(record.serialize())


@docs(
    tags=["issue-credential"],
    summary="Send holder a credential, automating entire flow",
)
@request_schema(V10CredentialProposalRequestMandSchema())
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_send(request: web.BaseRequest):
    """
    Request handler for sending credential from issuer to holder from attr values.

    If both issuer and holder are configured for automatic responses, the operation
    ultimately results in credential issue; otherwise, the result waits on the first
    response not automated; the credential exchange record retains state regardless.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    body = await request.json()

    comment = body.get("comment")
    connection_id = body.get("connection_id")
    preview_spec = body.get("credential_proposal")
    if not preview_spec:
        raise web.HTTPBadRequest(reason="credential_proposal must be provided")
    auto_remove = body.get("auto_remove")
    preview = CredentialPreview.deserialize(preview_spec)

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_proposal = CredentialProposal(
        comment=comment,
        credential_proposal=preview,
        **{t: body.get(t) for t in CRED_DEF_TAGS if body.get(t)},
    )

    trace_event(
        context.settings,
        credential_proposal,
        outcome="credential_exchange_send.START",
    )

    credential_manager = CredentialManager(context)

    (
        credential_exchange_record,
        credential_offer_message,
    ) = await credential_manager.prepare_send(
        connection_id,
        credential_proposal=credential_proposal,
        auto_remove=auto_remove,
    )
    await outbound_handler(
        credential_offer_message, connection_id=credential_exchange_record.connection_id
    )

    trace_event(
        context.settings,
        credential_offer_message,
        outcome="credential_exchange_send.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(tags=["issue-credential"], summary="Send issuer a credential proposal")
@request_schema(V10CredentialProposalRequestOptSchema())
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_send_proposal(request: web.BaseRequest):
    """
    Request handler for sending credential proposal.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    comment = body.get("comment")
    preview_spec = body.get("credential_proposal")
    preview = CredentialPreview.deserialize(preview_spec) if preview_spec else None
    auto_remove = body.get("auto_remove")

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_manager = CredentialManager(context)

    credential_exchange_record = await credential_manager.create_proposal(
        connection_id,
        comment=comment,
        credential_preview=preview,
        auto_remove=auto_remove,
        **{t: body.get(t) for t in CRED_DEF_TAGS if body.get(t)},
    )

    credential_proposal = CredentialProposal.deserialize(
            credential_exchange_record.credential_proposal_dict
        )
    await outbound_handler(
        credential_proposal,
        connection_id=connection_id,
    )

    trace_event(
        context.settings,
        credential_proposal,
        outcome="credential_exchange_send_proposal.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(
    tags=["issue-credential"],
    summary="Send holder a credential offer, independent of any proposal with preview",
)
@request_schema(V10CredentialOfferRequestSchema())
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_send_free_offer(request: web.BaseRequest):
    """
    Request handler for sending free credential offer.

    An issuer initiates a such a credential offer, free from any
    holder-initiated corresponding credential proposal with preview.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    body = await request.json()

    connection_id = body.get("connection_id")
    cred_def_id = body.get("cred_def_id")
    auto_issue = body.get(
        "auto_issue", context.settings.get("debug.auto_respond_credential_request")
    )
    auto_remove = body.get("auto_remove")
    comment = body.get("comment")
    preview_spec = body.get("credential_preview")

    if not cred_def_id:
        raise web.HTTPBadRequest(reason="cred_def_id is required")

    if auto_issue and not preview_spec:
        raise web.HTTPBadRequest(
            reason=(
                "If auto_issue is set then credential_preview must be provided"
            )
        )

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    if preview_spec:
        credential_preview = CredentialPreview.deserialize(preview_spec)
        credential_proposal = CredentialProposal(
            comment=comment,
            credential_proposal=credential_preview,
            cred_def_id=cred_def_id,
        )
        credential_proposal_dict = credential_proposal.serialize()
    else:
        credential_proposal_dict = None

    credential_exchange_record = V10CredentialExchange(
        connection_id=connection_id,
        initiator=V10CredentialExchange.INITIATOR_SELF,
        credential_definition_id=cred_def_id,
        credential_proposal_dict=credential_proposal_dict,
        auto_issue=auto_issue,
        auto_remove=auto_remove,
    )

    credential_manager = CredentialManager(context)

    (
        credential_exchange_record,
        credential_offer_message,
    ) = await credential_manager.create_offer(
        credential_exchange_record, comment=comment
    )

    await outbound_handler(credential_offer_message, connection_id=connection_id)

    trace_event(
        context.settings,
        credential_offer_message,
        outcome="credential_exchange_send_free_offer.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(
    tags=["issue-credential"],
    summary="Send holder a credential offer in reference to a proposal with preview",
)
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_send_bound_offer(request: web.BaseRequest):
    """
    Request handler for sending bound credential offer.

    A holder initiates this sequence with a credential proposal; this message
    responds with an offer bound to the proposal.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    credential_exchange_id = request.match_info["cred_ex_id"]
    credential_exchange_record = await V10CredentialExchange.retrieve_by_id(
        context, credential_exchange_id
    )
    assert credential_exchange_record.state == (
        V10CredentialExchange.STATE_PROPOSAL_RECEIVED
    )

    connection_id = credential_exchange_record.connection_id
    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_manager = CredentialManager(context)

    (
        credential_exchange_record,
        credential_offer_message,
    ) = await credential_manager.create_offer(credential_exchange_record, comment=None)

    await outbound_handler(credential_offer_message, connection_id=connection_id)

    trace_event(
        context.settings,
        credential_offer_message,
        outcome="credential_exchange_send_bound_offer.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(tags=["issue-credential"], summary="Send issuer a credential request")
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_send_request(request: web.BaseRequest):
    """
    Request handler for sending credential request.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    credential_exchange_id = request.match_info["cred_ex_id"]
    credential_exchange_record = await V10CredentialExchange.retrieve_by_id(
        context, credential_exchange_id
    )
    connection_id = credential_exchange_record.connection_id

    assert credential_exchange_record.state == (
        V10CredentialExchange.STATE_OFFER_RECEIVED
    )

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_manager = CredentialManager(context)

    (
        credential_exchange_record,
        credential_request_message,
    ) = await credential_manager.create_request(
        credential_exchange_record, connection_record.my_did
    )

    await outbound_handler(credential_request_message, connection_id=connection_id)

    trace_event(
        context.settings,
        credential_request_message,
        outcome="credential_exchange_send_request.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(tags=["issue-credential"], summary="Send holder a credential")
@request_schema(V10CredentialIssueRequestSchema())
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_issue(request: web.BaseRequest):
    """
    Request handler for sending credential.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    body = await request.json()
    comment = body.get("comment")
    preview_spec = body.get("credential_preview")

    if not preview_spec:
        raise web.HTTPBadRequest(reason="credential_preview must be provided")

    credential_exchange_id = request.match_info["cred_ex_id"]
    cred_exch_record = await V10CredentialExchange.retrieve_by_id(
        context, credential_exchange_id
    )
    connection_id = cred_exch_record.connection_id

    assert cred_exch_record.state == V10CredentialExchange.STATE_REQUEST_RECEIVED

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_preview = CredentialPreview.deserialize(preview_spec)

    credential_manager = CredentialManager(context)

    try:
        (
            cred_exch_record,
            credential_issue_message,
        ) = await credential_manager.issue_credential(
            cred_exch_record,
            comment=comment,
            credential_values=credential_preview.attr_dict(decode=False),
        )
    except IssuerRevocationRegistryFullError:
        raise web.HTTPBadRequest(reason="Revocation registry is full")

    await outbound_handler(credential_issue_message, connection_id=connection_id)

    trace_event(
        context.settings,
        credential_issue_message,
        outcome="credential_exchange_issue.END",
        perf_counter=r_time
    )

    return web.json_response(cred_exch_record.serialize())


@docs(tags=["issue-credential"], summary="Store a received credential")
@request_schema(V10CredentialStoreRequestSchema())
@response_schema(V10CredentialExchangeSchema(), 200)
async def credential_exchange_store(request: web.BaseRequest):
    """
    Request handler for storing credential.

    Args:
        request: aiohttp request object

    Returns:
        The credential exchange record

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    try:
        body = await request.json() or {}
        credential_id = body.get("credential_id")
    except JSONDecodeError:
        credential_id = None

    credential_exchange_id = request.match_info["cred_ex_id"]
    credential_exchange_record = await V10CredentialExchange.retrieve_by_id(
        context, credential_exchange_id
    )
    connection_id = credential_exchange_record.connection_id

    assert credential_exchange_record.state == (
        V10CredentialExchange.STATE_CREDENTIAL_RECEIVED
    )

    try:
        connection_record = await ConnectionRecord.retrieve_by_id(
            context, connection_id
        )
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    if not connection_record.is_ready:
        raise web.HTTPForbidden()

    credential_manager = CredentialManager(context)

    (
        credential_exchange_record,
        credential_stored_message,
    ) = await credential_manager.store_credential(
        credential_exchange_record, credential_id
    )

    await outbound_handler(credential_stored_message, connection_id=connection_id)

    trace_event(
        context.settings,
        credential_stored_message,
        outcome="credential_exchange_store.END",
        perf_counter=r_time
    )

    return web.json_response(credential_exchange_record.serialize())


@docs(
    tags=["issue-credential"],
    parameters=[
        {
            "name": "rev_reg_id",
            "in": "query",
            "description": "revocation registry id",
            "required": True
        },
        {
            "name": "cred_rev_id",
            "in": "query",
            "description": "credential revocation id",
            "required": True
        },
        {
            "name": "publish",
            "in": "query",
            "description": (
                "(true) publish revocation to ledger immediately, or "
                "(false) mark it pending"
            ),
            "schema": {"type": "boolean"},
            "required": False
        }
    ],
    summary="Revoke an issued credential"
)
async def credential_exchange_revoke(request: web.BaseRequest):
    """
    Request handler for storing a credential request.

    Args:
        request: aiohttp request object

    Returns:
        The credential request details.

    """
    context = request.app["request_context"]

    rev_reg_id = request.query.get("rev_reg_id")
    cred_rev_id = request.query.get("cred_rev_id")
    publish = bool(json.loads(request.query.get("publish", json.dumps(False))))

    credential_manager = CredentialManager(context)
    try:
        await credential_manager.revoke_credential(rev_reg_id, cred_rev_id, publish)
    except StorageNotFoundError:
        raise web.HTTPBadRequest()

    return web.json_response({})


@docs(tags=["issue-credential"], summary="Publish pending revocations to ledger")
@response_schema(V10PublishRevocationsResultSchema(), 200)
async def credential_exchange_publish_revocations(request: web.BaseRequest):
    """
    Request handler for publishing pending revocations to the ledger.

    Args:
        request: aiohttp request object

    Returns:
        Credential revocation ids published as revoked by revocation registry id.

    """
    context = request.app["request_context"]

    credential_manager = CredentialManager(context)

    return web.json_response(
        {
            "results": await credential_manager.publish_pending_revocations()
        }
    )


@docs(
    tags=["issue-credential"], summary="Remove an existing credential exchange record"
)
async def credential_exchange_remove(request: web.BaseRequest):
    """
    Request handler for removing a credential exchange record.

    Args:
        request: aiohttp request object

    """
    context = request.app["request_context"]
    credential_exchange_id = request.match_info["cred_ex_id"]
    try:
        credential_exchange_record = await V10CredentialExchange.retrieve_by_id(
            context, credential_exchange_id
        )
    except StorageNotFoundError:
        raise web.HTTPNotFound()
    await credential_exchange_record.delete_record(context)
    return web.json_response({})


@docs(
    tags=["issue-credential"], summary="Send a problem report for credential exchange"
)
@request_schema(V10CredentialProblemReportRequestSchema())
async def credential_exchange_problem_report(request: web.BaseRequest):
    """
    Request handler for sending problem report.

    Args:
        request: aiohttp request object

    """
    r_time = get_timer()

    context = request.app["request_context"]
    outbound_handler = request.app["outbound_message_router"]

    credential_exchange_id = request.match_info["cred_ex_id"]
    body = await request.json()

    try:
        credential_exchange_record = await V10CredentialExchange.retrieve_by_id(
            context, credential_exchange_id
        )
    except StorageNotFoundError:
        raise web.HTTPNotFound()

    error_result = ProblemReport(explain_ltxt=body["explain_ltxt"])
    error_result.assign_thread_id(credential_exchange_record.thread_id)

    await outbound_handler(
        error_result, connection_id=credential_exchange_record.connection_id
    )

    trace_event(
        context.settings,
        error_result,
        outcome="credential_exchange_problem_report.END",
        perf_counter=r_time
    )

    return web.json_response({})


async def register(app: web.Application):
    """Register routes."""

    app.add_routes(
        [
            web.get(
                "/issue-credential/mime-types/{credential_id}", attribute_mime_types_get
            ),
            web.get("/issue-credential/records", credential_exchange_list),
            web.get(
                "/issue-credential/records/{cred_ex_id}", credential_exchange_retrieve
            ),
            web.post("/issue-credential/send", credential_exchange_send),
            web.post(
                "/issue-credential/send-proposal", credential_exchange_send_proposal
            ),
            web.post(
                "/issue-credential/send-offer", credential_exchange_send_free_offer
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/send-offer",
                credential_exchange_send_bound_offer,
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/send-request",
                credential_exchange_send_request,
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/issue",
                credential_exchange_issue,
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/store",
                credential_exchange_store,
            ),
            web.post(
                "/issue-credential/revoke",
                credential_exchange_revoke,
            ),
            web.post(
                "/issue-credential/publish-revocations",
                credential_exchange_publish_revocations,
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/remove",
                credential_exchange_remove,
            ),
            web.post(
                "/issue-credential/records/{cred_ex_id}/problem-report",
                credential_exchange_problem_report,
            ),
        ]
    )
