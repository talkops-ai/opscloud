"""Textual widgets for OpsCloud.

Import directly from submodules, e.g.:

    from opscloud.ui.widgets.chat_input import ChatInput
    from opscloud.ui.widgets.messages import AssistantMessage, MessageList, UserMessage
    from opscloud.ui.widgets.diff import EnhancedDiff, compose_diff_lines
    from opscloud.ui.widgets.autocomplete import AutocompletePopup
    from opscloud.ui.widgets.status import StatusBar
    from opscloud.ui.widgets.subagent_panel import SubagentPanel
    from opscloud.ui.widgets.approval import ApprovalMenu, ApprovalModalScreen
    from opscloud.ui.widgets.ask_user import AskUserMenu
    from opscloud.ui.widgets.goal_review import GoalReviewMenu, GoalReviewScreen
    from opscloud.ui.widgets.model_selector import ModelSelectorScreen
    from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen
    from opscloud.ui.widgets.thread_selector import ThreadSelectorScreen
"""

from __future__ import annotations

from opscloud.ui.widgets.agent_selector import (
    AgentSelectorScreen,
)
from opscloud.ui.widgets.approval import (
    ApprovalDecided,
    ApprovalMenu,
    ApprovalModalScreen,
    assess_tool_risk,
)
from opscloud.ui.widgets.ask_user import (
    AskUserMenu,
    AskUserTextArea,
    OTHER_CHOICE_LABEL,
)
from opscloud.ui.widgets.auth import (
    AuthCheckbox,
    AuthPromptScreen,
    AuthResult,
    CONFIGURATION_DOCS_URL,
    PROVIDER_API_KEY_URLS,
    is_langsmith,
)
from opscloud.ui.widgets.auth_manager import (
    AuthManagerScreen,
)
from opscloud.ui.widgets.auto_mode_notice import (
    AUTO_MODE_DOCS_URL,
    AUTO_MODE_NOTICE_BODY,
    AutoModeNoticeScreen,
)
from opscloud.ui.widgets.autocomplete import (
    AutocompletePopup,
    CompletionController,
    CompletionResult,
    CompletionView,
    FuzzyFileController,
    MultiCompletionManager,
    SlashCommandController,
)
from opscloud.ui.widgets.chat_input import (
    ChatInput,
    ChatTextArea,
    InputMode,
    detect_input_mode,
)
from opscloud.ui.widgets.config_manager import (
    ConfigManagerScreen,
)
from opscloud.ui.widgets.debug_console import (
    DEBUG_TOGGLE_KEY,
    DebugConsoleScreen,
    SnapshotField,
)
from opscloud.ui.widgets.devops_renderers import (
    AnsiblePlaybookRenderer,
    HelmDiffRenderer,
    KubectlRenderer,
    TerraformPlanRenderer,
    TerraformPlanWidget,
)
from opscloud.ui.widgets.diff import (
    EnhancedDiff,
    compose_diff_lines,
)
from opscloud.ui.widgets.effort_selector import (
    EffortSelectorScreen,
)
from opscloud.ui.widgets.goal_review import (
    GoalReviewAccepted,
    GoalReviewCancelled,
    GoalReviewEdited,
    GoalReviewMenu,
    GoalReviewRejected,
    GoalReviewResult,
    GoalReviewScreen,
    GoalReviewTextArea,
)
from opscloud.ui.widgets.goal_status import (
    GoalStatusPanel,
)
from opscloud.ui.widgets.infra_panel import (
    InfraStatePanel,
)
from opscloud.ui.widgets.install_confirm import (
    InstallProviderConfirmScreen,
)
from opscloud.ui.widgets.loading import (
    BRAILLE_SPINNER_FRAMES,
    LoadingWidget,
    Spinner,
    format_duration,
)
from opscloud.ui.widgets.mcp_viewer import (
    MCPServerErrorScreen,
    MCPServerHeaderItem,
    MCPToolItem,
    MCPViewerScreen,
    MCP_VIEWER_RECONNECT_REQUEST,
)
from opscloud.ui.widgets.messages import (
    AppMessage,
    AssistantMessage,
    DiffMessage,
    ErrorMessage,
    FormattedOutput,
    MessageList,
    QueuedUserMessage,
    RubricResultMessage,
    SkillMessage,
    SystemMessage,
    ThinkingMessage,
    ToolCallMessage,
    ToolGroupSummary,
    UserMessage,
    summarize_live_tool_group,
    summarize_tool_group,
)
from opscloud.ui.widgets.cloud_selector import (
    CloudProfileOption,
    CloudProfileSelectorScreen,
)
from opscloud.ui.widgets.model_selector import (
    ModelOption,
    ModelSelectorScreen,
)
from opscloud.ui.widgets.notification_center import (
    NotificationCenter,
)
from opscloud.ui.widgets.notification_settings import (
    NotificationSettingsScreen,
    WARNING_TOGGLES,
)
from opscloud.ui.widgets.operation_card import (
    OperationCard,
)
from opscloud.ui.widgets.permissions_manager import (
    PermissionsManagerScreen,
)
from opscloud.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    PluginTabLabel,
    PluginTabSelected,
)
from opscloud.ui.widgets.pool_selector import (
    PoolSelectorScreen,
)
from opscloud.ui.widgets.preamble import (
    _mode_color,
)
from opscloud.ui.widgets.skills_viewer import (
    SkillDetailScreen,
    SkillItemWidget,
    SkillsViewerScreen,
)
from opscloud.ui.widgets.status import (
    CONNECTION_STATES,
    BranchLabel,
    ConnectionState,
    ModelLabel,
    PROVIDER_PREFIX_STRIPS,
    StatusBar,
)
from opscloud.ui.widgets.subagent_panel import (
    SubagentColumn,
    SubagentPanel,
    SubagentStatus,
    _Phase,
    _SubagentRecord,
    _format_timing,
    sanitize_control_chars,
)
from opscloud.ui.widgets.theme_selector import (
    ThemeSelectorScreen,
)
from opscloud.ui.widgets.thread_selector import (
    CustomOptionList,
    SearchInput,
    ThreadSelector,
    ThreadSelectorScreen,
)
from opscloud.ui.widgets.toast import (
    ToastNotification,
    show_toast,
)
from opscloud.ui.widgets.tool_display import (
    MAX_ARG_LENGTH,
    abbreviate_path,
    format_tool_display,
    format_tool_result_summary,
    get_tool_display_name,
    register_tool_display_name,
    register_tool_summary_formatter,
    truncate_value,
)
from opscloud.ui.widgets.tool_renderers import (
    DeleteFileRenderer,
    EditFileRenderer,
    TaskRenderer,
    ToolRenderer,
    ToolRendererResult,
    WriteFileRenderer,
    get_renderer,
    render_tool_approval,
)
from opscloud.ui.widgets.tool_widgets import (
    EditFileApprovalWidget,
    GenericApprovalWidget,
    TaskApprovalWidget,
    ToolApprovalWidget,
    WriteFileApprovalWidget,
    format_display_content,
)
from opscloud.ui.widgets.welcome import (
    MAX_DISPLAY_ITEMS,
    MAX_INLINE_CHARS,
    WelcomeBanner,
)
from opscloud.ui.widgets.welcome_popup import (
    WelcomeDetailPopup,
)

__all__ = [
    "AUTO_MODE_DOCS_URL",
    "AUTO_MODE_NOTICE_BODY",
    "AgentSelectorScreen",
    "AnsiblePlaybookRenderer",
    "AppMessage",
    "ApprovalDecided",
    "ApprovalMenu",
    "ApprovalModalScreen",
    "AskUserMenu",
    "AskUserTextArea",
    "AssistantMessage",
    "AuthCheckbox",
    "AuthManagerScreen",
    "AuthPromptScreen",
    "AuthResult",
    "AutocompletePopup",
    "AutoModeNoticeScreen",
    "BRAILLE_SPINNER_FRAMES",
    "BranchLabel",
    "CONFIGURATION_DOCS_URL",
    "CONNECTION_STATES",
    "ChatInput",
    "ChatTextArea",
    "CloudProfileOption",
    "CloudProfileSelectorScreen",
    "CompletionController",
    "CompletionResult",
    "CompletionView",
    "ConfigManagerScreen",
    "ConnectionState",
    "CustomOptionList",
    "DEBUG_TOGGLE_KEY",
    "DebugConsoleScreen",
    "DeleteFileRenderer",
    "DiffMessage",
    "EditFileApprovalWidget",
    "EditFileRenderer",
    "EffortSelectorScreen",
    "EnhancedDiff",
    "ErrorMessage",
    "FormattedOutput",
    "FuzzyFileController",
    "GenericApprovalWidget",
    "GoalReviewAccepted",
    "GoalReviewCancelled",
    "GoalReviewEdited",
    "GoalReviewMenu",
    "GoalReviewRejected",
    "GoalReviewResult",
    "GoalReviewScreen",
    "GoalReviewTextArea",
    "GoalStatusPanel",
    "HelmDiffRenderer",
    "InfraStatePanel",
    "InputMode",
    "InstallProviderConfirmScreen",
    "KubectlRenderer",
    "LoadingWidget",
    "MAX_ARG_LENGTH",
    "MAX_DISPLAY_ITEMS",
    "MAX_INLINE_CHARS",
    "MCPServerErrorScreen",
    "MCPServerHeaderItem",
    "MCPToolItem",
    "MCPViewerScreen",
    "MCP_VIEWER_RECONNECT_REQUEST",
    "MessageList",
    "ModelLabel",
    "ModelOption",
    "ModelSelectorScreen",
    "MultiCompletionManager",
    "NotificationCenter",
    "NotificationSettingsScreen",
    "OTHER_CHOICE_LABEL",
    "OperationCard",
    "PROVIDER_API_KEY_URLS",
    "PROVIDER_PREFIX_STRIPS",
    "PermissionsManagerScreen",
    "PluginManagerScreen",
    "PluginTabLabel",
    "PluginTabSelected",
    "PoolSelectorScreen",
    "QueuedUserMessage",
    "RubricResultMessage",
    "SearchInput",
    "SkillDetailScreen",
    "SkillItemWidget",
    "SkillMessage",
    "SkillsViewerScreen",
    "SlashCommandController",
    "SnapshotField",
    "Spinner",
    "StatusBar",
    "SubagentColumn",
    "SubagentPanel",
    "SubagentStatus",
    "SystemMessage",
    "TaskApprovalWidget",
    "TaskRenderer",
    "TerraformPlanRenderer",
    "TerraformPlanWidget",
    "ThemeSelectorScreen",
    "ThinkingMessage",
    "ThreadSelector",
    "ThreadSelectorScreen",
    "ToastNotification",
    "ToolApprovalWidget",
    "ToolCallMessage",
    "ToolGroupSummary",
    "ToolRenderer",
    "ToolRendererResult",
    "UserMessage",
    "WARNING_TOGGLES",
    "WelcomeBanner",
    "WelcomeDetailPopup",
    "WriteFileApprovalWidget",
    "WriteFileRenderer",
    "_Phase",
    "_SubagentRecord",
    "_format_timing",
    "_mode_color",
    "abbreviate_path",
    "assess_tool_risk",
    "compose_diff_lines",
    "detect_input_mode",
    "format_display_content",
    "format_duration",
    "format_tool_display",
    "format_tool_result_summary",
    "get_renderer",
    "get_tool_display_name",
    "is_langsmith",
    "register_tool_display_name",
    "register_tool_summary_formatter",
    "render_tool_approval",
    "sanitize_control_chars",
    "show_toast",
    "summarize_live_tool_group",
    "summarize_tool_group",
    "truncate_value",
]
