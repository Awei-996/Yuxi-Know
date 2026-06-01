from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware

from yuxi.agents import BaseAgent, BaseState, load_chat_model
from yuxi.agents.backends import create_agent_filesystem_middleware
from yuxi.agents.buildin.chatbot.prompt import TODO_MID_PROMPT, build_prompt_with_context
from yuxi.agents.buildin.subagent.context import SubAgentContext
from yuxi.agents.context import prepare_agent_runtime_context
from yuxi.agents.middlewares import create_summary_middleware, save_attachments_to_fs
from yuxi.agents.middlewares.knowledge_base_middleware import KnowledgeBaseMiddleware
from yuxi.agents.middlewares.skills_middleware import SkillsMiddleware
from yuxi.agents.toolkits.service import resolve_configured_runtime_tools


async def _build_middlewares(context):
    summary_trigger_tokens = getattr(context, "summary_threshold", 100) * 1024
    summary_middleware = create_summary_middleware(
        model=load_chat_model(fully_specified_name=context.model),
        trigger=("tokens", summary_trigger_tokens),
        keep=("tokens", summary_trigger_tokens // 2),
        trim_tokens_to_summarize=4000,
    )

    return [
        create_agent_filesystem_middleware(
            getattr(context, "tool_token_limit", 20) * 1024,
            context=context,
        ),
        save_attachments_to_fs,
        KnowledgeBaseMiddleware(),
        SkillsMiddleware(),
        summary_middleware,
        TodoListMiddleware(system_prompt=TODO_MID_PROMPT),
        PatchToolCallsMiddleware(),
        ModelRetryMiddleware(),
    ]


class SubAgentBackend(BaseAgent):
    name = "子智能体"
    description = "用于被主智能体通过 task 工具调用的专用智能体后端。"
    capabilities = ["file_upload", "files"]
    context_schema = SubAgentContext

    async def get_graph(self, context=None, **kwargs):
        context = await prepare_agent_runtime_context(
            context or self.context_schema(),
            context_schema=self.context_schema,
        )

        return create_agent(
            model=load_chat_model(fully_specified_name=context.model),
            tools=await resolve_configured_runtime_tools(context),
            system_prompt=build_prompt_with_context(context),
            middleware=await _build_middlewares(context),
            state_schema=BaseState,
            checkpointer=await self._get_checkpointer(),
        )
