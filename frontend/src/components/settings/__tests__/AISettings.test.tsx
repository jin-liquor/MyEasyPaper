import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AISettings from "../AISettings";
import api from "@/lib/api";
import { useFeatures } from "@/lib/features";
import { clearSession, startSession } from "@/lib/session";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const emptySettings = { providers: [], default_model_id: null };
const credentialReady = { ready: true, code: "CREDENTIAL_ENCRYPTION_READY", message: "server private detail" };
const configuredSettings = {
  providers: [{
    id: "provider-1", name: "Example", protocol: "responses", base_url: "https://api.example.test/v1", enabled: true,
    secret_configured: true,
    models: [{ id: "model-1", model_name: "example-model", context_window: 32000, max_output_tokens: 2048,
      input_types: ["text"], reasoning: { supported: false, levels: [], default_level: null, parameter: "reasoning_effort" } }],
  }],
  default_model_id: "model-1",
};
const mockedApi = vi.mocked(api);

describe("AISettings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReset();
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : emptySettings }));
  });

  it("loads the credential status independently and shows the loading state", async () => {
    let resolveStatus!: (value: { data: typeof credentialReady }) => void;
    mockedApi.get.mockImplementation((url) => url === "/api/settings/ai/credential-status"
      ? new Promise((resolve) => { resolveStatus = resolve; })
      : Promise.resolve({ data: emptySettings }));
    render(<AISettings />);
    expect(screen.getByText("正在检查")).toBeInTheDocument();
    expect(mockedApi.get).toHaveBeenCalledWith("/api/settings/ai/credential-status");
    resolveStatus({ data: credentialReady });
    expect(await screen.findByText("已就绪")).toBeInTheDocument();
    expect(screen.queryByText("server private detail")).not.toBeInTheDocument();
  });

  it("shows not-ready guidance for server administrators without claiming provider status", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status"
      ? { ready: false, code: "CREDENTIAL_ENCRYPTION_NOT_READY", message: "secret server exception" }
      : emptySettings }));
    const { container } = render(<AISettings />);
    expect(await screen.findByText("尚未就绪")).toBeInTheDocument();
    expect(screen.getByText(/保留旧版本，完成凭据重封装并核实全部记录后/)).toBeInTheDocument();
    expect(screen.getByText(/服务器密钥环尚未就绪/)).toBeInTheDocument();
    expect(container).not.toHaveTextContent("secret server exception");
    expect(container).not.toHaveTextContent("CREDENTIAL_ENCRYPTION_NOT_READY");
  });

  it("shows safe unavailable state and retries the credential status request", async () => {
    mockedApi.get.mockImplementationOnce(async (url) => {
      if (url === "/api/settings/ai/credential-status") throw new Error("private server exception");
      return { data: emptySettings };
    }).mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status"
      ? credentialReady
      : emptySettings }));
    const { container } = render(<AISettings />);
    expect(await screen.findByText("暂时无法确认")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("private server exception");
    fireEvent.click(screen.getByRole("button", { name: "重试状态检查" }));
    expect(await screen.findByText("已就绪")).toBeInTheDocument();
    expect(mockedApi.get).toHaveBeenCalledWith("/api/settings/ai/credential-status");
  });

  it("treats a non-contract status response as unavailable and never echoes its payload", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status"
      ? { ready: true, code: "wrong-code", message: "private exception", key_id: "private-id" }
      : emptySettings }));
    const { container } = render(<AISettings />);
    expect(await screen.findByText("暂时无法确认")).toBeInTheDocument();
    expect(container).not.toHaveTextContent("private exception");
    expect(container).not.toHaveTextContent("private-id");
    expect(container).not.toHaveTextContent("wrong-code");
  });

  it("loads an empty state and creates a provider without rendering its secret", async () => {
    mockedApi.post.mockResolvedValue({ data: configuredSettings });
    const { container } = render(<AISettings />);
    expect(await screen.findByText("尚未配置供应商。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("供应商名称"), { target: { value: "Example" } });
    fireEvent.change(screen.getByLabelText("协议"), { target: { value: "responses" } });
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com/v1"), { target: { value: "https://api.example.test/v1" } });
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "secret-test-value" } });
    fireEvent.click(screen.getByRole("button", { name: "添加供应商" }));
    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith("/api/settings/ai/providers", {
      name: "Example", protocol: "responses", base_url: "https://api.example.test/v1", api_key: "secret-test-value",
    }));
    expect(await screen.findAllByText("example-model")).not.toHaveLength(0);
    expect(container).not.toHaveTextContent("secret-test-value");
    expect(screen.getByLabelText("设置或轮换 Example 的 API Key")).toHaveValue("");
  });

  it("shows masked secret status and rotates the key without displaying it", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    mockedApi.post.mockResolvedValue({ data: { secret_configured: true } });
    const { container } = render(<AISettings />);
    expect(await screen.findByText("API Key 已设置（仅显示状态）")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("设置或轮换 Example 的 API Key"), { target: { value: "replacement-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "更新密钥" }));
    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith("/api/settings/ai/providers/provider-1/secret", { api_key: "replacement-secret" }));
    await waitFor(() => expect(mockedApi.get).toHaveBeenCalledTimes(3));
    expect(container).not.toHaveTextContent("replacement-secret");
    expect(screen.getByLabelText("设置或轮换 Example 的 API Key")).toHaveValue("");
    expect(screen.getByRole("button", { name: "清除Example的 API Key" })).toBeInTheDocument();
  });

  it("refreshes current-session AI readiness only after saving the default model succeeds", async () => {
    startSession("settings-test-token", "a@example.test");
    let saved = false;
    mockedApi.get.mockImplementation(async (url) => {
      if (url === "/api/auth/features") return { data: { ai_available: saved } };
      if (url === "/api/settings/ai/credential-status") return { data: credentialReady };
      return { data: { ...configuredSettings, default_model_id: null } };
    });
    mockedApi.patch.mockRejectedValueOnce(new Error("save failed")).mockImplementationOnce(async () => {
      saved = true;
      return { data: { default_model_id: "model-1" } };
    });
    function Readiness() {
      const features = useFeatures();
      return <output aria-label="AI readiness">{features.loaded ? String(features.ai_available) : "loading"}</output>;
    }
    const view = render(<><AISettings /><Readiness /></>);
    try {
      const readiness = screen.getByLabelText("AI readiness");
      await waitFor(() => expect(readiness).toHaveTextContent("false"));
      const select = await screen.findByLabelText("默认模型");
      fireEvent.change(select, { target: { value: "model-1" } });
      await waitFor(() => expect(select).toBeEnabled());
      expect(mockedApi.get.mock.calls.filter(([url]) => url === "/api/auth/features")).toHaveLength(1);
      fireEvent.change(select, { target: { value: "model-1" } });
      await waitFor(() => expect(readiness).toHaveTextContent("true"));
      expect(mockedApi.patch).toHaveBeenLastCalledWith("/api/settings/ai/default-model", { model_id: "model-1" });
      expect(mockedApi.get.mock.calls.filter(([url]) => url === "/api/auth/features")).toHaveLength(2);
    } finally {
      view.unmount();
      clearSession();
    }
  });

  it("disables model saving when output exceeds the context window", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: /^编辑$/ }));
    fireEvent.change(screen.getByLabelText("上下文窗口（tokens）"), { target: { value: "1000" } });
    expect(screen.getByText("最大输出不能大于上下文窗口。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存模型" })).toBeDisabled();
  });

  it("discovers candidates and pre-fills manual capability configuration", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    mockedApi.post.mockResolvedValue({ data: { candidates: [
      { id: "new-model", model_name: "new-model", needs_manual_capabilities: true },
      { id: "other-model", model_name: "other-model", needs_manual_capabilities: true },
    ], warnings: [] } });
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith("/api/settings/ai/providers/provider-1/models/discover", { apply: false, model_ids: [] }));
    fireEvent.change(await screen.findByLabelText("筛选模型"), { target: { value: "new" } });
    expect(screen.getByText("new-model")).toBeInTheDocument();
    expect(screen.queryByText("other-model")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "new-model" }));
    fireEvent.click(screen.getByRole("button", { name: "手工配置所选 ID" }));
    expect(screen.getByLabelText("模型 ID / 名称")).toHaveValue("new-model");
    expect(screen.getByLabelText("上下文窗口（tokens）")).toHaveValue(128000);
    expect(screen.getByText(/请核对上下文、最大输出和输入能力/)).toBeInTheDocument();
    expect(mockedApi.post).toHaveBeenCalledTimes(1);
  });

  it("supports discovery empty and safe error states", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    mockedApi.post.mockResolvedValueOnce({ data: { candidates: [], warnings: [] } });
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: "发现模型" }));
    expect(await screen.findByText("目录为空；可继续手动添加模型。")).toBeInTheDocument();
    mockedApi.post.mockRejectedValueOnce({ response: { data: { detail: { code: "CREDENTIAL_KEY_MISCONFIGURED", message: "private stack secret" } } } });
    fireEvent.click(screen.getByRole("button", { name: "发现模型" }));
    expect(await screen.findByText(/服务器凭据加密密钥缺失或无效/)).toBeInTheDocument();
    expect(screen.getByText(/服务器密钥环当前可加载/)).toBeInTheDocument();
    expect(screen.queryByText("private stack secret")).not.toBeInTheDocument();
  });

  it("does not send a test request when confirmation is cancelled", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: "测试连接" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("供应商可能按其定价计费"));
    expect(mockedApi.post).not.toHaveBeenCalled();
  });

  it("tests the selected model after confirmation and displays latency", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    vi.mocked(window.confirm).mockReturnValue(true);
    mockedApi.post.mockResolvedValue({ data: { latency_ms: 123 } });
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(mockedApi.post).toHaveBeenCalledWith("/api/settings/ai/providers/provider-1/test", { model_id: "model-1" }));
    expect(await screen.findByText("连接成功 · 123 ms")).toBeInTheDocument();
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("最多 16 个输出 token"));
  });

  it("does not render raw provider exception text", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    vi.mocked(window.confirm).mockReturnValue(true);
    mockedApi.post.mockRejectedValue({ response: { data: { detail: "private stack secret" } } });
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: "测试连接" }));
    expect(await screen.findByText("连接测试失败。")).toBeInTheDocument();
    expect(screen.queryByText("private stack secret")).not.toBeInTheDocument();
  });
  it("initializes the fixed reasoning defaults when support is enabled", async () => {
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : configuredSettings }));
    render(<AISettings />);
    fireEvent.click(await screen.findByRole("button", { name: /^编辑$/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: "支持思考强度" }));
    expect(screen.getByRole("button", { name: "思考档位" })).toHaveTextContent("low, high, max");
    expect(screen.getByLabelText("默认思考强度")).toHaveValue("");
  });
  it("keeps stored reasoning values while selecting levels and saving the existing model payload", async () => {
    const settings = {
      ...configuredSettings,
      providers: [{ ...configuredSettings.providers[0], models: [{
        ...configuredSettings.providers[0].models[0],
        reasoning: { supported: true, levels: [{ label: "旧中档", api_value: "medium" }, { label: "未展示的供应商档位", api_value: "vendor_custom" }], default_level: "旧中档", parameter: "reasoning.effort" },
      }] }],
    };
    mockedApi.get.mockImplementation(async (url) => ({ data: url === "/api/settings/ai/credential-status" ? credentialReady : settings }));
    mockedApi.patch.mockResolvedValue({ data: settings.providers[0].models[0] });
    render(<AISettings />);
    await screen.findByText(/思考：medium$/);
    expect(screen.getByText(/思考：medium$/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /^编辑$/ }));
    const picker = screen.getByRole("button", { name: "思考档位" });
    expect(picker).toHaveTextContent("medium");
    expect(picker).not.toHaveTextContent("未展示的供应商档位");
    fireEvent.pointerDown(picker, { button: 0, ctrlKey: false, pointerType: "mouse" });
    expect(await screen.findByRole("menuitemcheckbox", { name: "medium" })).toBeChecked();
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(7);
    expect(screen.queryByText("未展示的供应商档位")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "low" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "low" })).toBeChecked();
    fireEvent.keyDown(screen.getByRole("menuitemcheckbox", { name: "low" }), { key: "Escape" });
    fireEvent.change(screen.getByLabelText("默认思考强度"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型" }));
    await waitFor(() => expect(mockedApi.patch).toHaveBeenCalledWith("/api/settings/ai/models/model-1", expect.objectContaining({
      reasoning: { supported: true, levels: [{ label: "旧中档", api_value: "medium" }, { label: "未展示的供应商档位", api_value: "vendor_custom" }, { label: "low", api_value: "low" }], default_level: null, parameter: "reasoning.effort" },
    })));
  });
});
