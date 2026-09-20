"""Official Python MCP server composition and protocol-clean stdio launch."""

from __future__ import annotations

import json
import logging
import sys
from typing import Annotated, Any, Literal

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent, ToolAnnotations
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from cove_sensory_mcp.errors import ErrorCode, SensoryError, error_result
from cove_sensory_mcp.models import DetailLevel, Modality, ProviderId
from cove_sensory_mcp.services import AppServices
from cove_sensory_mcp.tools.audio import sense_audio
from cove_sensory_mcp.tools.image import sense_image
from cove_sensory_mcp.tools.inputs import (
    SenseAudioInput,
    SenseImageInput,
    SenseMusicInput,
    SenseVideoInput,
)
from cove_sensory_mcp.tools.music import sense_music
from cove_sensory_mcp.tools.setup import (
    sensory_self_test,
    sensory_setup_guide,
    sensory_status,
)
from cove_sensory_mcp.tools.video import sense_video

RequestedModality = Literal["image", "video_visual", "video_audio", "audio", "music"]

class OpenAIFile(BaseModel):
    """ChatGPT file parameter shape documented by the OpenAI Plugins runtime."""

    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


_FILE_PARAM_META = {"openai/fileParams": ["file"]}


def _media_source(source: str | None, file: OpenAIFile | None) -> str:
    if file is not None:
        return file.download_url
    if source:
        return source
    raise ToolError("A media file or source URL is required.")


def _require_unique_modalities(
    modalities: list[RequestedModality],
) -> list[RequestedModality]:
    if len(set(modalities)) != len(modalities):
        raise ValueError("modalities must be unique")
    return modalities


RequestedModalities = Annotated[
    list[RequestedModality],
    Field(
        min_length=1,
        max_length=len(Modality),
        json_schema_extra={"uniqueItems": True},
    ),
    AfterValidator(_require_unique_modalities),
]

_SAFE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_SELF_TEST_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)
_SENSING_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_SENSING_DESCRIPTION = (
    "Read caller-authorized media and send it to the user's configured sensory Provider; "
    "the original media is never modified or stored permanently."
)
_STATUS_DESCRIPTION = "Inspect local configuration status; read-only setup tools never accept credentials."
_GUIDE_DESCRIPTION = "Inspect local configuration setup options; read-only setup tools never accept credentials."
_SELF_TEST_DESCRIPTION = (
    "Inspect local configuration readiness by sending tiny test media to the configured "
    "Provider; this may use a small amount of Provider quota and update verified state. "
    "This tool never accepts credentials."
)
_INVALID_ARGUMENTS_MESSAGE = "The tool arguments are invalid."


def _invalid_arguments_result() -> CallToolResult:
    payload = error_result(
        SensoryError(ErrorCode.CONFIG_INVALID, _INVALID_ARGUMENTS_MESSAGE)
    )
    return CallToolResult(
        content=[
            TextContent(
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        ],
        structured_content=payload,
        is_error=True,
    )


class _PrivacySafeFastMCP(FastMCP[None]):
    """Translate SDK tool failures before their raw details reach an MCP client."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[None, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError:
            return _invalid_arguments_result()


def create_server(services: AppServices, **server_kwargs: Any) -> FastMCP[None]:
    """Bind the foundation setup handlers to the official Python MCP server."""
    apps = Apps()
    server: FastMCP[None] = _PrivacySafeFastMCP(
        "cove-sensory-mcp", extensions=[apps], **server_kwargs
    )

    audio_bridge_html = r"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font:14px system-ui;margin:0;padding:12px;color:CanvasText;background:Canvas}
#s{white-space:pre-wrap;line-height:1.45}.muted{opacity:.7}
</style></head>
<body><div id="s" class="muted">正在把附件交给耳朵…</div>
<script>
(async()=>{
  const el=document.getElementById("s");
  try{
    const input=window.openai?.toolInput;
    const file=input?.file;
    if(!file?.file_id) throw new Error("没有收到 ChatGPT file_id");
    if(!window.openai?.getFileDownloadUrl) throw new Error("当前客户端不支持临时文件下载 URL");
    if(!window.openai?.callTool) throw new Error("当前客户端不支持组件调用工具");
    const fresh=await window.openai.getFileDownloadUrl({fileId:file.file_id});
    if(!fresh?.downloadUrl) throw new Error("没有取得临时下载 URL");
    el.textContent="耳朵已拿到附件，正在听…";
    const out=await window.openai.callTool("sense_audio_from_chatgpt_url",{
      source:fresh.downloadUrl,
      question:input?.question||"",
      start_seconds:input?.start_seconds??null,
      end_seconds:input?.end_seconds??null,
      detail:input?.detail||"auto",
      include_transcript:input?.include_transcript??true,
      language:input?.language||"zh-CN"
    });
    el.textContent="听完了。";
    const payload=out?.structuredContent ?? out?.content ?? out;
    if(window.openai?.sendFollowUpMessage){
      await window.openai.sendFollowUpMessage({
        prompt:"耳朵已经完成音频分析。请根据下面的 Cove 工具结果直接回答我刚才的问题，不要再次调用文件桥。\n\n"+JSON.stringify(payload),
        scrollToBottom:true
      });
    }
  }catch(e){
    el.textContent="附件交接失败："+(e?.message||String(e));
  }
})();
</script></body></html>"""
    apps.add_html_resource(
        "ui://cove/audio-bridge-v1.html",
        audio_bridge_html,
        title="Cove audio bridge",
        description="Refresh a ChatGPT file URL in the client before Cove downloads it.",
        csp=ResourceCsp(connect_domains=[]),
        prefers_border=True,
    )

    @server.tool(
        name="sensory_status",
        description=_STATUS_DESCRIPTION,
        annotations=_SAFE_ANNOTATIONS,
    )
    async def status_tool() -> dict[str, object]:
        return await sensory_status(services)

    @server.tool(
        name="sensory_setup_guide",
        description=_GUIDE_DESCRIPTION,
        annotations=_SAFE_ANNOTATIONS,
    )
    async def setup_guide_tool() -> dict[str, object]:
        return await sensory_setup_guide(services)

    @server.tool(
        name="sensory_self_test",
        description=_SELF_TEST_DESCRIPTION,
        annotations=_SELF_TEST_ANNOTATIONS,
    )
    async def self_test_tool(modalities: RequestedModalities) -> dict[str, object]:
        return await sensory_self_test(
            services, [Modality(modality) for modality in modalities]
        )

    @server.tool(
        name="sense_image",
        description=_SENSING_DESCRIPTION,
        annotations=_SENSING_ANNOTATIONS,
    )
    async def image_tool(
        source: str,
        question: str = "",
        detail: DetailLevel = DetailLevel.AUTO,
        language: str = "zh-CN",
        provider: ProviderId | None = None,
    ) -> CallToolResult:
        return await sense_image(
            services,
            SenseImageInput(
                source=source,
                question=question,
                detail=detail,
                language=language,
                provider=provider,
            ),
        )

    @server.tool(
        name="sense_video",
        description=_SENSING_DESCRIPTION,
        annotations=_SENSING_ANNOTATIONS,
        meta=_FILE_PARAM_META,
    )
    async def video_tool(
        file: OpenAIFile,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        detail: DetailLevel = DetailLevel.AUTO,
        include_audio: bool = True,
        language: str = "zh-CN",
        visual_provider: ProviderId | None = None,
        audio_provider: ProviderId | None = None,
    ) -> CallToolResult:
        return await sense_video(
            services,
            SenseVideoInput(
                source=file.download_url,
                question=question,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                detail=detail,
                include_audio=include_audio,
                language=language,
                visual_provider=visual_provider,
                audio_provider=audio_provider,
            ),
        )

    @apps.tool(
        resource_uri="ui://cove/audio-bridge-v1.html",
        name="sense_audio_chatgpt_file",
        description=(
            "Use this for an audio attachment uploaded in ChatGPT. The small client bridge "
            "refreshes the ChatGPT file URL, then hands the temporary URL to Cove for analysis."
        ),
        annotations=_SENSING_ANNOTATIONS,
        meta=_FILE_PARAM_META,
    )
    async def audio_chatgpt_file_tool(
        file: OpenAIFile,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        detail: DetailLevel = DetailLevel.AUTO,
        include_transcript: bool = True,
        language: str = "zh-CN",
    ) -> dict[str, object]:
        return {
            "status": "bridge_ready",
            "file_id": file.file_id,
            "file_name": file.file_name,
            "mime_type": file.mime_type,
        }

    @server.tool(
        name="sense_audio_from_chatgpt_url",
        description="App-only helper: analyze a fresh temporary ChatGPT file download URL.",
        annotations=_SENSING_ANNOTATIONS,
        meta={
            "ui": {"visibility": ["app"]},
            "openai/widgetAccessible": True,
            "openai/visibility": "private",
        },
    )
    async def audio_from_chatgpt_url_tool(
        source: str,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        detail: DetailLevel = DetailLevel.AUTO,
        include_transcript: bool = True,
        language: str = "zh-CN",
    ) -> CallToolResult:
        if not source.startswith("https://"):
            raise ToolError("A temporary HTTPS file URL is required.")
        return await sense_audio(
            services,
            SenseAudioInput(
                source=source,
                question=question,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                detail=detail,
                include_transcript=include_transcript,
                language=language,
            ),
        )

    @server.tool(
        name="sense_audio",
        description=_SENSING_DESCRIPTION,
        annotations=_SENSING_ANNOTATIONS,
        meta=_FILE_PARAM_META,
    )
    async def audio_tool(
        file: OpenAIFile,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        detail: DetailLevel = DetailLevel.AUTO,
        include_transcript: bool = True,
        language: str = "zh-CN",
    ) -> CallToolResult:
        return await sense_audio(
            services,
            SenseAudioInput(
                source=file.download_url,
                question=question,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                detail=detail,
                include_transcript=include_transcript,
                language=language,
            ),
        )

    @server.tool(
        name="sense_music",
        description=_SENSING_DESCRIPTION,
        annotations=_SENSING_ANNOTATIONS,
    )
    async def music_tool(
        source: str,
        question: str = "",
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        detail: DetailLevel = DetailLevel.AUTO,
        include_lyrics_transcript: bool = False,
        language: str = "zh-CN",
    ) -> CallToolResult:
        return await sense_music(
            services,
            SenseMusicInput(
                source=source,
                question=question,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                detail=detail,
                include_lyrics_transcript=include_lyrics_transcript,
                language=language,
            ),
        )

    return server


def run_stdio(services: AppServices) -> None:
    """Run the MCP loop with every ordinary diagnostic directed to stderr."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    logging.getLogger(__name__).info("Starting cove-sensory-mcp stdio server")
    create_server(services).run(transport="stdio")
