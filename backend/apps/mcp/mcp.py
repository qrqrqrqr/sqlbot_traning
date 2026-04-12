# Author: Junjun
# Date: 2025/7/1
import json
from datetime import timedelta
from typing import Optional

import jwt
from fastapi import HTTPException, status, APIRouter
# from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlmodel import select

from apps.chat.api.chat import create_chat, question_answer_inner
from apps.chat.models.chat_model import ChatMcp, CreateChat, ChatStart, McpQuestion, McpAssistant, ChatQuestion, \
    ChatFinishStep, McpDs, McpDrpoPrepare, McpDrpoScore
from apps.chat.task.llm import LLMService
from apps.datasource.crud.datasource import get_datasource_list
from apps.system.crud.user import authenticate, user_ws_options
from apps.system.crud.user import get_db_user
from apps.system.models.system_model import UserWsModel
from apps.system.models.user import UserModel
from apps.system.schemas.system_schema import BaseUserDTO, AssistantHeader
from apps.system.schemas.system_schema import UserInfoDTO
from common.core import security
from common.core.config import settings
from common.core.deps import SessionDep, Trans
from common.core.schemas import TokenPayload, XOAuth2PasswordBearer, Token
from common.core.security import create_access_token

reusable_oauth2 = XOAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)

router = APIRouter(tags=["mcp"], prefix="/mcp")


def parse_datasource_id(datasource_id: Optional[int | str]) -> Optional[int]:
    if datasource_id is None:
        return None
    if isinstance(datasource_id, str):
        if datasource_id.strip() == "":
            return None
        try:
            return int(datasource_id.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid datasource ID") from exc
    if isinstance(datasource_id, int):
        return datasource_id
    raise HTTPException(status_code=400, detail="Invalid datasource ID")


# @router.post("/access_token", operation_id="access_token")
# def local_login(
#         session: SessionDep,
#         form_data: Annotated[OAuth2PasswordRequestForm, Depends()]
# ) -> Token:
#     user = authenticate(session=session, account=form_data.username, password=form_data.password)
#     if not user:
#         raise HTTPException(status_code=400, detail="Incorrect account or password")
#     access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
#     user_dict = user.to_dict()
#     return Token(access_token=create_access_token(
#         user_dict, expires_delta=access_token_expires
#     ))


def get_user(session: SessionDep, token: str):
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except (InvalidTokenError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    # session_user = await get_user_info(session=session, user_id=token_data.id)

    db_user: UserModel = get_db_user(session=session, user_id=token_data.id)
    session_user = UserInfoDTO.model_validate(db_user.model_dump())
    session_user.isAdmin = session_user.id == 1 and session_user.account == 'admin'
    session_user.language = 'zh-CN'
    if session_user.isAdmin:
        session_user = session_user
    ws_model: UserWsModel = session.exec(
        select(UserWsModel).where(UserWsModel.uid == session_user.id, UserWsModel.oid == session_user.oid)).first()
    session_user.weight = ws_model.weight if ws_model else -1

    session_user = UserInfoDTO.model_validate(session_user)
    if not session_user:
        raise HTTPException(status_code=404, detail="User not found")

    if session_user.status != 1:
        raise HTTPException(status_code=400, detail="Inactive user")
    return session_user


@router.post("/mcp_start", operation_id="mcp_start")
async def mcp_start(session: SessionDep, chat: ChatStart):
    user: BaseUserDTO = authenticate(session=session, account=chat.username, password=chat.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect account or password")

    if not user.oid or user.oid == 0:
        raise HTTPException(status_code=400, detail="No associated workspace, Please contact the administrator")
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    user_dict = user.to_dict()
    t = Token(access_token=create_access_token(
        user_dict, expires_delta=access_token_expires
    ))
    c = create_chat(session, user, CreateChat(origin=1), False)
    return {"access_token": t.access_token, "chat_id": c.id}


@router.post("/mcp_ws_list", operation_id="mcp_ws_list")
async def ws_list(session: SessionDep, trans: Trans, token: str):
    session_user = get_user(session, token)
    return await user_ws_options(session, session_user.id, trans)


@router.post("/mcp_ds_list", operation_id="mcp_datasource_list")
async def datasource_list(session: SessionDep, mcp_ds: McpDs):
    session_user = get_user(session, mcp_ds.token)
    if mcp_ds.oid:
        session_user.oid = int(mcp_ds.oid)
    ds_list = get_datasource_list(session=session, user=session_user)
    result = []
    for item in ds_list:
        dic = item.__dict__
        dic.pop('embedding', None)
        dic.pop('table_relation', None)
        dic.pop('recommended_config', None)
        dic.pop('configuration', None)
        result.append(dic)
    return result


#
#
# @router.get("/model_list", operation_id="get_model_list")
# async def get_model_list(session: SessionDep):
#     return session.query(AiModelDetail).all()


@router.post("/mcp_question", operation_id="mcp_question")
async def mcp_question(session: SessionDep, chat: McpQuestion):
    session_user = get_user(session, chat.token)
    if chat.oid:
        session_user.oid = int(chat.oid)
    ds_id = parse_datasource_id(chat.datasource_id)

    mcp_chat = ChatMcp(token=chat.token, chat_id=chat.chat_id, question=chat.question, datasource_id=ds_id)
    # Forward evaluation toggles so external callers can run the same ablation matrix
    # as the internal /chat/question/eval endpoint.
    mcp_chat.disable_terms = bool(chat.disable_terms)
    mcp_chat.disable_sql_examples = bool(chat.disable_sql_examples)
    mcp_chat.disable_custom_prompt = bool(chat.disable_custom_prompt)
    mcp_chat.include_debug_payload = bool(chat.include_debug_payload)
    mcp_chat.include_log_history = bool(chat.include_log_history)

    finish_step = chat.finish_step if chat.finish_step is not None else ChatFinishStep.GENERATE_CHART

    return await question_answer_inner(session=session, current_user=session_user, request_question=mcp_chat,
                                       in_chat=False, stream=chat.stream, finish_step=finish_step)


@router.post("/mcp_drpo_prepare", operation_id="mcp_drpo_prepare")
async def mcp_drpo_prepare(session: SessionDep, chat: McpDrpoPrepare):
    session_user = get_user(session, chat.token)
    if chat.oid:
        session_user.oid = int(chat.oid)

    ds_id = parse_datasource_id(chat.datasource_id)
    request_question = ChatQuestion(chat_id=chat.chat_id, question=chat.question, datasource_id=ds_id)
    request_question.disable_terms = bool(chat.disable_terms)
    request_question.disable_sql_examples = bool(chat.disable_sql_examples)
    request_question.disable_custom_prompt = bool(chat.disable_custom_prompt)
    request_question.include_debug_payload = bool(chat.include_debug_payload)

    llm_service = await LLMService.create(session, session_user, request_question, None, embedding=True)
    if not llm_service.ds:
        raise HTTPException(status_code=400, detail="No datasource selected for DRPO prepare")

    oid = llm_service.ds.oid if hasattr(llm_service.ds, 'oid') else 1
    ds_context_id = llm_service.ds.id if hasattr(llm_service.ds, 'id') else None
    prompt_pack = llm_service.prepare_sql_prompt_pack(session, oid, ds_context_id)

    return {
        "success": True,
        "mode": "online_drpo_prepare",
        "chat_id": chat.chat_id,
        "datasource_id": ds_context_id,
        "prompt_pack": prompt_pack,
    }


@router.post("/mcp_drpo_score", operation_id="mcp_drpo_score")
async def mcp_drpo_score(session: SessionDep, chat: McpDrpoScore):
    session_user = get_user(session, chat.token)
    if chat.oid:
        session_user.oid = int(chat.oid)

    ds_id = parse_datasource_id(chat.datasource_id)
    request_question = ChatQuestion(chat_id=chat.chat_id, question=chat.question, datasource_id=ds_id)
    request_question.disable_terms = bool(chat.disable_terms)
    request_question.disable_sql_examples = bool(chat.disable_sql_examples)
    request_question.disable_custom_prompt = bool(chat.disable_custom_prompt)
    request_question.include_debug_payload = bool(chat.include_debug_payload)

    llm_service = await LLMService.create(session, session_user, request_question, None, embedding=True)
    if not llm_service.ds:
        raise HTTPException(status_code=400, detail="No datasource selected for DRPO scoring")

    oid = llm_service.ds.oid if hasattr(llm_service.ds, 'oid') else 1
    ds_context_id = llm_service.ds.id if hasattr(llm_service.ds, 'id') else None
    prompt_pack = llm_service.prepare_sql_prompt_pack(session, oid, ds_context_id)
    score_result = llm_service.score_sql_candidates(
        session,
        gold_sql=chat.gold_sql,
        candidate_sqls=chat.candidate_sqls,
        float_places=int(chat.float_places or 4),
        max_preview_rows=int(chat.max_preview_rows or 5),
        reward_config=chat.reward.model_dump() if chat.reward else None,
    )

    return {
        "success": True,
        "mode": "online_drpo_score",
        "chat_id": chat.chat_id,
        "datasource_id": ds_context_id,
        "prompt_pack": prompt_pack if chat.include_debug_payload else None,
        "score_result": score_result,
    }


# Cordys crm
@router.post("/mcp_assistant", operation_id="mcp_assistant")
async def mcp_assistant(session: SessionDep, chat: McpAssistant):
    session_user = BaseUserDTO(**{
        "id": -1, "account": 'sqlbot-mcp-assistant', "oid": 1, "assistant_id": -1, "password": '', "language": "zh-CN"
    })
    # session_user: UserModel = get_db_user(session=session, user_id=1)
    # session_user.oid = 1
    c = create_chat(session, session_user, CreateChat(origin=1), False)

    # build assistant param
    configuration = {"endpoint": chat.url}
    # authorization = [{"key": "x-de-token",
    #                 "value": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjEsIm9pZCI6MSwiZXhwIjoxNzU4NTEyMDA2fQ.3NR-pgnADLdXZtI3dXX5-LuxfGYRvYD9kkr2de7KRP0",
    #                 "target": "header"}]
    mcp_assistant_header = AssistantHeader(id=1, name='mcp_assist', domain='', type=1,
                                           configuration=json.dumps(configuration),
                                           certificate=chat.authorization)

    # assistant question
    mcp_chat = ChatQuestion(chat_id=c.id, question=chat.question)
    # ask
    return await question_answer_inner(session=session, current_user=session_user, request_question=mcp_chat,
                                       current_assistant=mcp_assistant_header,
                                       in_chat=False, stream=chat.stream, finish_step=ChatFinishStep.QUERY_DATA)
