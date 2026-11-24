import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Plus, RefreshCw, Save, Search, Trash2 } from "lucide-react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { toast } from "sonner";
import api from "@/lib/api";
import { invalidateFeaturesCache } from "@/lib/features";
import { useSession } from "@/lib/session";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";

type Protocol = "chat_completions" | "responses";
type ReasoningLevel = { label: string; api_value: string };
type Model = {
  id: string;
  model_name: string;
  context_window: number;
  max_output_tokens: number;
  input_types: ("text" | "image")[];
  reasoning: { supported: boolean; levels: ReasoningLevel[]; default_level?: string | null; parameter?: string };
  needs_manual_capabilities?: boolean;
};
type Provider = {
  id: string;
  name: string;
  protocol: Protocol;
  base_url: string;
  enabled: boolean;
  secret_configured: boolean;
  models: Model[];
};
type SettingsData = { providers: Provider[]; default_model_id: string | null };
type ModelDraft = Omit<Model, "id">;
type DiscoveredModel = { id: string; model_name: string; needs_manual_capabilities: boolean };
type DiscoveryState = { loading: boolean; applying: boolean; candidates: DiscoveredModel[]; selected: string[]; query: string; error: string; warning: string };
type TestState = { loading: boolean; error: string; latencyMs: number | null; selectedModel?: string };
type CredentialState = "loading" | "ready" | "not-ready" | "unavailable";
type CredentialStatusResponse = { ready: boolean; code: string };

const REASONING_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"] as const;
const DEFAULT_REASONING_LEVELS: ReasoningLevel[] = ["low", "high", "max"].map((value) => ({ label: value, api_value: value }));
const reasoningLevelLabel = (level: ReasoningLevel) => REASONING_LEVELS.includes(level.api_value as (typeof REASONING_LEVELS)[number])
  ? level.api_value
  : level.label;

function ReasoningLevelPicker({ levels, onChange }: { levels: ReasoningLevel[]; onChange: (levels: ReasoningLevel[]) => void }) {
  const standard = REASONING_LEVELS.map((value) => levels.find((item) => item.api_value === value) ?? ({ label: value, api_value: value }));
  const toggle = (item: ReasoningLevel, checked: boolean) => {
    const next = checked
      ? [...levels.filter((level) => level.api_value !== item.api_value), item]
      : levels.filter((level) => level.api_value !== item.api_value);
    onChange(next);
  };

  return <DropdownMenu.Root>
    <DropdownMenu.Trigger asChild>
      <Button type="button" variant="secondary" className="ai-reasoning-picker" aria-label="思考档位">
        {levels.filter((level) => REASONING_LEVELS.includes(level.api_value as (typeof REASONING_LEVELS)[number])).length
          ? levels.filter((level) => REASONING_LEVELS.includes(level.api_value as (typeof REASONING_LEVELS)[number])).map((level) => reasoningLevelLabel(level)).join(", ")
          : "选择思考档位"}<ChevronDown aria-hidden="true" />
      </Button>
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content className="ui-menu ai-reasoning-menu" align="start" sideOffset={4} aria-label="可选思考档位">
        <DropdownMenu.Label className="ui-menu-label">思考档位</DropdownMenu.Label>
        {standard.map((level) => <DropdownMenu.CheckboxItem key={level.api_value} className="ui-menu-item ai-reasoning-menu-item" checked={levels.some((item) => item.api_value === level.api_value)} onCheckedChange={(checked) => toggle(level, checked)} onSelect={(event) => event.preventDefault()}>
          <span className="ai-reasoning-check"><DropdownMenu.ItemIndicator><Check aria-hidden="true" /></DropdownMenu.ItemIndicator></span>{reasoningLevelLabel(level)}
        </DropdownMenu.CheckboxItem>)}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}

const CREDENTIAL_READY_CODE = "CREDENTIAL_ENCRYPTION_READY";
const CREDENTIAL_NOT_READY_CODE = "CREDENTIAL_ENCRYPTION_NOT_READY";

const errorCode = (error: unknown): string => {
  const value = error as { response?: { data?: { detail?: { code?: unknown } } } };
  const code = value?.response?.data?.detail?.code;
  return typeof code === "string" ? code : "";
};
const safeErrorText = (error: unknown, fallback: string) => {
  const messages: Record<string, string> = {
    CREDENTIAL_KEY_MISCONFIGURED: "服务器凭据加密密钥缺失或无效。请联系服务器管理员检查服务器端配置并重启服务；不要在此页面提交根密钥。",
    CREDENTIAL_DECRYPTION_FAILED: "已保存的账户凭据无法解密。请联系服务器管理员检查密钥版本配置；不要在此页面提交根密钥。",
    AI_NOT_CONFIGURED: "该供应商尚未配置 API Key 或已停用。",
    AI_UPSTREAM_AUTH_FAILED: "供应商鉴权失败，请检查账户 API Key。",
    AI_UPSTREAM_RATE_LIMITED: "供应商请求频率受限，请稍后重试。",
    AI_UPSTREAM_UNAVAILABLE: "供应商暂时不可用，请稍后重试。",
    AI_UPSTREAM_TIMEOUT: "连接供应商超时，请检查服务状态后重试。",
    AI_UPSTREAM_UNREACHABLE: "无法连接供应商，请检查地址和网络。",
    AI_UPSTREAM_PROTOCOL_ERROR: "供应商返回了无法识别的响应。",
    MODEL_DISCOVERY_UNSUPPORTED: "供应商不支持模型目录读取，请手动添加模型。",
    MODEL_NOT_FOUND: "所选模型已不存在，请重新加载设置。",
    PROTOCOL_UNSUPPORTED: "当前供应商协议不受支持。",
    AI_BASE_URL_FORBIDDEN: "供应商地址不符合安全要求，请检查 HTTPS 地址。",
  };
  const code = errorCode(error);
  if (code === "CREDENTIAL_KEY_MISCONFIGURED") return messages[code];
  if (code === "CREDENTIAL_DECRYPTION_FAILED") return messages[code];
  return messages[code] || fallback;
};

const isCredentialStatusResponse = (value: unknown): value is CredentialStatusResponse => {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<CredentialStatusResponse>;
  return typeof result.ready === "boolean"
    && ((result.ready && result.code === CREDENTIAL_READY_CODE)
      || (!result.ready && result.code === CREDENTIAL_NOT_READY_CODE));
};

const blankModel = (): ModelDraft => ({
  model_name: "", context_window: 128000, max_output_tokens: 4096, input_types: ["text"],
  reasoning: { supported: false, levels: [], parameter: "reasoning_effort" },
});
const emptyDiscovery = (): DiscoveryState => ({ loading: false, applying: false, candidates: [], selected: [], query: "", error: "", warning: "" });

export default function AISettings() {
  const session = useSession();
  const refreshCapabilities = () => invalidateFeaturesCache(session);
  const [data, setData] = useState<SettingsData>({ providers: [], default_model_id: null });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [providerDraft, setProviderDraft] = useState({ name: "", protocol: "chat_completions" as Protocol, base_url: "", api_key: "" });
  const [editingProvider, setEditingProvider] = useState<string | null>(null);
  const [editProviderDraft, setEditProviderDraft] = useState({ name: "", protocol: "chat_completions" as Protocol, base_url: "" });
  const [secretDraft, setSecretDraft] = useState({ providerId: "", value: "" });
  const [modelDraft, setModelDraft] = useState<ModelDraft>(blankModel);
  const [editingModel, setEditingModel] = useState<string | null>(null);
  const [modelProvider, setModelProvider] = useState("");
  const [discoveries, setDiscoveries] = useState<Record<string, DiscoveryState>>({});
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [credentialState, setCredentialState] = useState<CredentialState>("loading");
  const credentialRequestRef = useRef(0);

  const refreshCredentialStatus = useCallback(async () => {
    const requestId = ++credentialRequestRef.current;
    setCredentialState("loading");
    let nextState: CredentialState = "unavailable";
    try {
      const response = await api.get("/api/settings/ai/credential-status");
      if (isCredentialStatusResponse(response.data)) nextState = response.data.ready ? "ready" : "not-ready";
    } catch {
      nextState = "unavailable";
    }
    if (requestId === credentialRequestRef.current) setCredentialState(nextState);
  }, []);

  useEffect(() => {
    void refreshCredentialStatus();
    return () => { credentialRequestRef.current += 1; };
  }, [refreshCredentialStatus]);

  const load = useCallback(async () => {
    try { const response = await api.get("/api/settings/ai"); setData(response.data as SettingsData); setLoadError(""); }
    catch (error) { setLoadError(safeErrorText(error, "无法加载 AI 设置，请重试。")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    let active = true;
    api.get("/api/settings/ai")
      .then((response) => { if (active) setData(response.data as SettingsData); })
      .catch((error: unknown) => { if (active) setLoadError(safeErrorText(error, "无法加载 AI 设置，请重试。")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  const reload = () => { setLoading(true); setLoadError(""); void load(); };
  const models = useMemo(() => data.providers.flatMap((provider) => provider.models.map((model) => ({ provider, model }))), [data.providers]);
  const saveResult = (result: SettingsData) => {
    setData(result);
    refreshCapabilities();
  };

  const addProvider = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!providerDraft.name.trim() || !providerDraft.base_url.trim() || !providerDraft.api_key.trim()) return;
    setSaving(true);
    try {
      const response = await api.post("/api/settings/ai/providers", { name: providerDraft.name.trim(), protocol: providerDraft.protocol, base_url: providerDraft.base_url.trim(), api_key: providerDraft.api_key });
      saveResult(response.data); setProviderDraft({ name: "", protocol: "chat_completions", base_url: "", api_key: "" }); toast.success("AI 供应商已添加");
    } catch (error) { toast.error(safeErrorText(error, "添加供应商失败")); }
    finally { setProviderDraft((current) => ({ ...current, api_key: "" })); setSaving(false); }
  };
  const updateProvider = async (provider: Provider, patch: Partial<Pick<Provider, "name" | "protocol" | "base_url" | "enabled">>): Promise<boolean> => {
    setSaving(true);
    try { const response = await api.patch(`/api/settings/ai/providers/${provider.id}`, patch); saveResult(response.data); toast.success("供应商设置已保存"); return true; }
    catch (error) { toast.error(safeErrorText(error, "保存供应商失败")); return false; }
    finally { setSaving(false); }
  };
  const deleteProvider = async (provider: Provider) => {
    if (!window.confirm(`删除供应商“${provider.name}”及其模型？`)) return;
    setSaving(true);
    try { const response = await api.delete(`/api/settings/ai/providers/${provider.id}`); saveResult(response.data); toast.success("供应商已删除"); }
    catch (error) { toast.error(safeErrorText(error, "删除供应商失败；请先更换默认模型。")); }
    finally { setSaving(false); }
  };
  const rotateSecret = async (provider: Provider) => {
    const secret = secretDraft.providerId === provider.id ? secretDraft.value : "";
    if (!secret.trim()) return;
    setSaving(true);
    try { await api.post(`/api/settings/ai/providers/${provider.id}/secret`, { api_key: secret }); refreshCapabilities(); setSecretDraft({ providerId: "", value: "" }); await load(); toast.success("API Key 已更新"); }
    catch (error) { setSecretDraft({ providerId: "", value: "" }); toast.error(safeErrorText(error, "更新 API Key 失败")); }
    finally { setSaving(false); }
  };
  const clearSecret = async (provider: Provider) => {
    if (!window.confirm(`清除“${provider.name}”的 API Key？`)) return;
    setSaving(true);
    try { await api.delete(`/api/settings/ai/providers/${provider.id}/secret`); refreshCapabilities(); await load(); toast.success("API Key 已清除"); }
    catch (error) { toast.error(safeErrorText(error, "清除 API Key 失败")); }
    finally { setSaving(false); }
  };
  const saveModel = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!modelProvider || !modelDraft.model_name.trim()) return;
    const payload = { ...modelDraft, model_name: modelDraft.model_name.trim() };
    setSaving(true);
    try {
      if (editingModel) await api.patch(`/api/settings/ai/models/${editingModel}`, payload);
      else await api.post("/api/settings/ai/models", { ...payload, provider_id: modelProvider });
      refreshCapabilities(); setModelDraft(blankModel()); setEditingModel(null); await load(); toast.success(editingModel ? "模型已更新" : "模型已添加");
    } catch (error) { toast.error(safeErrorText(error, "保存模型失败")); }
    finally { setSaving(false); }
  };
  const deleteModel = async (model: Model) => {
    if (!window.confirm(`删除模型“${model.model_name}”？`)) return;
    setSaving(true);
    try { await api.delete(`/api/settings/ai/models/${model.id}`); refreshCapabilities(); await load(); toast.success("模型已删除"); }
    catch (error) { toast.error(safeErrorText(error, "删除模型失败；请先更换默认模型。")); }
    finally { setSaving(false); }
  };
  const setDefault = async (modelId: string) => {
    setSaving(true);
    try { const response = await api.patch("/api/settings/ai/default-model", { model_id: modelId }); refreshCapabilities(); setData((current) => ({ ...current, default_model_id: response.data.default_model_id })); }
    catch (error) { toast.error(safeErrorText(error, "设置默认模型失败")); }
    finally { setSaving(false); }
  };
  const discoverModels = async (provider: Provider) => {
    setDiscoveries((current) => ({ ...current, [provider.id]: { ...(current[provider.id] || emptyDiscovery()), loading: true, error: "", warning: "" } }));
    try {
      const response = await api.post(`/api/settings/ai/providers/${provider.id}/models/discover`, { apply: false, model_ids: [] });
      const result = response.data as { candidates?: DiscoveredModel[]; warnings?: string[] };
      setDiscoveries((current) => ({ ...current, [provider.id]: { ...(current[provider.id] || emptyDiscovery()), loading: false, candidates: result.candidates || [], selected: [], error: "", warning: (result.warnings || []).join(" ") } }));
    } catch (error) {
      setDiscoveries((current) => ({ ...current, [provider.id]: { ...(current[provider.id] || emptyDiscovery()), loading: false, error: safeErrorText(error, "模型目录读取失败；可继续手动添加模型。") } }));
    }
  };
  const applyDiscoveredModels = async (provider: Provider) => {
    const state = discoveries[provider.id] || emptyDiscovery();
    const candidate = state.candidates.find((item) => state.selected.includes(item.id));
    if (!candidate) return;
    setModelProvider(provider.id);
    setEditingModel(null);
    setModelDraft({ ...blankModel(), model_name: candidate.model_name });
    setDiscoveries((current) => ({ ...current, [provider.id]: { ...state, warning: "模型 ID 已填入手动配置表单；请核对上下文、最大输出和输入能力后再保存。" } }));
    window.requestAnimationFrame(() => {
      const form = document.getElementById("ai-model-form");
      if (form && typeof form.scrollIntoView === "function") form.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  };
  const testProvider = async (provider: Provider, modelId: string) => {
    const model = provider.models.find((item) => item.id === modelId);
    if (!model || model.needs_manual_capabilities) return;
    const confirmed = window.confirm(`将使用“${model.model_name}”发送一条最小测试请求（最多 ${Math.min(16, model.max_output_tokens)} 个输出 token）。供应商可能按其定价计费。是否继续？`);
    if (!confirmed) return;
    setTests((current) => ({ ...current, [provider.id]: { loading: true, error: "", latencyMs: null } }));
    try {
      const response = await api.post(`/api/settings/ai/providers/${provider.id}/test`, { model_id: modelId });
      setTests((current) => ({ ...current, [provider.id]: { loading: false, error: "", latencyMs: response.data.latency_ms } }));

    } catch (error) {
      setTests((current) => ({ ...current, [provider.id]: { loading: false, error: safeErrorText(error, "连接测试失败。"), latencyMs: null } }));
    }
  };
  const editModel = (provider: Provider, model: Model) => {
    setModelProvider(provider.id); setEditingModel(model.id);
    setModelDraft({ model_name: model.model_name, context_window: model.context_window, max_output_tokens: model.max_output_tokens, input_types: [...model.input_types], reasoning: { ...model.reasoning, levels: [...(model.reasoning.levels || [])] } });
    const form = document.getElementById("ai-model-form");
    if (form && typeof form.scrollIntoView === "function") form.scrollIntoView({ block: "nearest" });
  };

  return <div className="settings-group ai-settings" aria-busy={loading || saving}>
    <div className="settings-option settings-option-stack">
      <div className="ai-settings-heading"><div><h3>账户 AI 供应商</h3><p>配置仅作用于当前账户。API Key 由服务端加密保存，页面不会再次显示密钥。</p></div>
        <Button variant="secondary" size="sm" onClick={reload} disabled={loading || saving} aria-label="重新加载 AI 设置"><RefreshCw />重新加载</Button></div>
      <div className={`ai-crypto-status is-${credentialState}`} role={credentialState === "unavailable" ? "alert" : "status"} aria-live="polite">
        <strong>服务器凭据加密：</strong>{credentialState === "loading" ? "正在检查" : credentialState === "ready" ? "已就绪" : credentialState === "not-ready" ? "尚未就绪" : "暂时无法确认"}
        {credentialState === "loading" && <span>正在读取服务器密钥环状态，请稍候。</span>}
        {credentialState === "ready" && <span>服务器密钥环当前可加载。此状态不代表当前账户已配置 API Key、已有凭据可解密或供应商连接正常。</span>}
        {credentialState === "not-ready" && <><span>服务器密钥环尚未就绪。请联系管理员在服务端检查 EASYPAPER_CREDENTIAL_ENCRYPTION_KEYS 与 EASYPAPER_CREDENTIAL_ENCRYPTION_ACTIVE_VERSION，并完成验证；不要在网页生成或提交根密钥。</span><span>密钥轮换须由管理员在服务端执行：保留旧版本，完成凭据重封装并核实全部记录后，才能移除旧版本。</span></>}
        {credentialState === "unavailable" && <><span>无法读取服务器凭据加密状态。请稍后重试；此状态不代表当前账户或供应商配置状态。</span><Button variant="secondary" size="sm" onClick={() => void refreshCredentialStatus()}>重试状态检查</Button></>}
      </div>
      {loading ? <p role="status"><Loader2 className="spin" /> 正在加载 AI 设置…</p> : loadError ? <div className="ai-error" role="alert"><span>{loadError}</span><Button variant="secondary" onClick={reload}>重试</Button></div> : <>
        {data.providers.length === 0 ? <p className="ai-empty">尚未配置供应商。</p> : <div className="ai-provider-list">
          {data.providers.map((provider) => <article className="ai-provider" key={provider.id}>
            <div className="ai-provider-top"><div><h4>{provider.name}</h4><p>{provider.protocol === "responses" ? "Responses" : "Chat Completions"} · {provider.base_url}</p></div>
              <div className="ai-provider-actions"><label className="ai-enabled"><span>启用</span><Switch checked={provider.enabled} onCheckedChange={(enabled) => void updateProvider(provider, { enabled })} disabled={saving} aria-label={`启用${provider.name}`} /></label>
                <Button variant="destructive" size="icon-sm" onClick={() => void deleteProvider(provider)} disabled={saving} aria-label={`删除${provider.name}`} title="删除供应商"><Trash2 /></Button></div></div>
            {editingProvider === provider.id ? <form className="ai-form" onSubmit={(event) => { event.preventDefault(); void updateProvider(provider, editProviderDraft).then((saved) => { if (saved) setEditingProvider(null); }); }}>
              <label>供应商名称<Input required value={editProviderDraft.name} onChange={(e) => setEditProviderDraft({ ...editProviderDraft, name: e.target.value })} /></label>
              <label>协议<Select value={editProviderDraft.protocol} onChange={(e) => setEditProviderDraft({ ...editProviderDraft, protocol: e.target.value as Protocol })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></Select></label>
              <label className="ai-wide">API 地址<Input required type="url" value={editProviderDraft.base_url} onChange={(e) => setEditProviderDraft({ ...editProviderDraft, base_url: e.target.value })} /></label>
              <div className="settings-actions"><Button type="submit" disabled={saving}><Save />保存</Button><Button type="button" variant="secondary" onClick={() => setEditingProvider(null)}>取消</Button></div>
            </form> : <Button variant="secondary" size="sm" onClick={() => { setEditingProvider(provider.id); setEditProviderDraft({ name: provider.name, protocol: provider.protocol, base_url: provider.base_url }); }}>编辑供应商</Button>}
            <div className="ai-secret-row"><span>{provider.secret_configured ? "API Key 已设置（仅显示状态）" : <span className="settings-warning">尚未设置 API Key</span>}</span>
              <div className="ai-secret-controls"><label className="sr-only" htmlFor={`secret-${provider.id}`}>设置或轮换 {provider.name} 的 API Key</label>
                <Input id={`secret-${provider.id}`} type="password" autoComplete="new-password" value={secretDraft.providerId === provider.id ? secretDraft.value : ""} onChange={(e) => setSecretDraft({ providerId: provider.id, value: e.target.value })} placeholder="输入新 API Key" aria-label={`设置或轮换 ${provider.name} 的 API Key`} />
                <Button size="sm" onClick={() => void rotateSecret(provider)} disabled={saving || secretDraft.providerId !== provider.id || !secretDraft.value.trim()}><Save />更新密钥</Button>
                {provider.secret_configured && <Button size="icon-sm" variant="secondary" onClick={() => void clearSecret(provider)} disabled={saving} aria-label={`清除${provider.name}的 API Key`} title="清除 API Key"><Trash2 /></Button>}</div></div>
            <div className="ai-model-list"><div className="ai-subheading"><h5>模型 ({provider.models.length})</h5></div>
              {provider.models.map((model) => <div className="ai-model-row" key={model.id}><div><strong>{model.model_name}</strong><small>上下文 {model.context_window.toLocaleString()} · 最大输出 {model.max_output_tokens.toLocaleString()} · {model.input_types.join("、")} · 思考{model.reasoning.supported ? `：${model.reasoning.levels.filter((level) => REASONING_LEVELS.includes(level.api_value as (typeof REASONING_LEVELS)[number])).map((level) => reasoningLevelLabel(level)).join("/")}` : "：不支持"}</small></div>
                <div className="ai-model-actions">{data.default_model_id === model.id && <span className="pill is-ok">默认</span>}<Button variant="secondary" size="sm" onClick={() => editModel(provider, model)}>编辑</Button><Button variant="destructive" size="icon-sm" onClick={() => void deleteModel(model)} aria-label={`删除模型 ${model.model_name}`} title="删除模型" disabled={saving}><Trash2 /></Button></div></div>)}
              {provider.models.length === 0 && <p className="ai-empty">该供应商还没有模型，请手动添加。</p>}
             </div>
             <div className="ai-provider-tools">
               <div className="ai-tool-heading"><h5>模型目录</h5><Button variant="secondary" size="sm" onClick={() => void discoverModels(provider)} disabled={Boolean(discoveries[provider.id]?.loading) || !provider.enabled || !provider.secret_configured}><Search />{discoveries[provider.id]?.loading ? "正在读取…" : "发现模型"}</Button></div>
               <p className="ai-capability-note">目录读取可能需要有效 API Key；目录只返回 ID，加入后仍需手动补齐能力。失败时可手动添加。</p>
               {discoveries[provider.id]?.error && <p className="ai-error" role="alert">{discoveries[provider.id].error}</p>}
               {discoveries[provider.id]?.warning && <p className="ai-capability-note" role="status">{discoveries[provider.id].warning}</p>}
               {discoveries[provider.id]?.candidates.length > 0 && <>
                 <label className="ai-discovery-filter">筛选模型<Input value={discoveries[provider.id].query} onChange={(event) => setDiscoveries((current) => ({ ...current, [provider.id]: { ...current[provider.id], query: event.target.value } }))} placeholder="输入模型 ID" /></label>
                 <div className="ai-candidate-list">{discoveries[provider.id].candidates.filter((candidate) => candidate.model_name.toLowerCase().includes(discoveries[provider.id].query.trim().toLowerCase())).map((candidate) => <label key={candidate.id} className="ai-candidate"><input type="checkbox" checked={discoveries[provider.id].selected.includes(candidate.id)} onChange={(event) => setDiscoveries((current) => { const state = current[provider.id]; return { ...current, [provider.id]: { ...state, selected: event.target.checked ? [...state.selected, candidate.id] : state.selected.filter((id) => id !== candidate.id) } }; })} /><span>{candidate.model_name}</span>{provider.models.some((model) => model.id === candidate.id) && <small>已添加</small>}</label>)}
                   {discoveries[provider.id].candidates.filter((candidate) => candidate.model_name.toLowerCase().includes(discoveries[provider.id].query.trim().toLowerCase())).length === 0 && <p className="ai-empty">无匹配结果；可清除筛选或手动添加。</p>}
                 </div>
                 <Button size="sm" onClick={() => void applyDiscoveredModels(provider)} disabled={discoveries[provider.id].applying || !discoveries[provider.id].selected.length}><Check />手工配置所选 ID</Button>
               </>}
               {discoveries[provider.id] && !discoveries[provider.id].loading && !discoveries[provider.id].error && discoveries[provider.id].candidates.length === 0 && <p className="ai-empty" role="status">目录为空；可继续手动添加模型。</p>}
               <div className="ai-test-row"><label>测试模型<Select aria-label={`连接测试模型 ${provider.name}`} value={provider.models.some((model) => model.id === tests[provider.id]?.selectedModel && !model.needs_manual_capabilities) ? tests[provider.id]?.selectedModel || "" : provider.models.find((model) => !model.needs_manual_capabilities)?.id || ""} onChange={(event) => setTests((current) => ({ ...current, [provider.id]: { ...(current[provider.id] || { loading: false, error: "", latencyMs: null }), selectedModel: event.target.value } }))} disabled={!provider.models.some((model) => !model.needs_manual_capabilities)}><option value="">选择已配置能力的模型</option>{provider.models.filter((model) => !model.needs_manual_capabilities).map((model) => <option key={model.id} value={model.id}>{model.model_name}</option>)}</Select></label>
                 <Button size="sm" onClick={() => void testProvider(provider, tests[provider.id]?.selectedModel || provider.models.find((model) => !model.needs_manual_capabilities)?.id || "")} disabled={!provider.enabled || !provider.secret_configured || !provider.models.some((model) => !model.needs_manual_capabilities) || Boolean(tests[provider.id]?.loading)}><Check />{tests[provider.id]?.loading ? "正在测试…" : "测试连接"}</Button>
                 {tests[provider.id]?.latencyMs !== null && tests[provider.id] && !tests[provider.id].error && <span className="ai-test-success" role="status">连接成功 · {tests[provider.id].latencyMs} ms</span>}
                 {tests[provider.id]?.error && <span className="ai-error" role="alert">{tests[provider.id].error}</span>}
               </div>
             </div>
           </article>)}
        </div>}
        <form className="ai-form ai-add-provider" onSubmit={(event) => void addProvider(event)}>
          <h4>添加供应商</h4>
          <label>供应商名称<Input required maxLength={100} value={providerDraft.name} onChange={(e) => setProviderDraft({ ...providerDraft, name: e.target.value })} /></label>
          <label>协议<Select value={providerDraft.protocol} onChange={(e) => setProviderDraft({ ...providerDraft, protocol: e.target.value as Protocol })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></Select></label>
          <label>API 地址<Input required type="url" placeholder="https://api.example.com/v1" value={providerDraft.base_url} onChange={(e) => setProviderDraft({ ...providerDraft, base_url: e.target.value })} /></label>
          <label>API Key<Input required type="password" autoComplete="new-password" value={providerDraft.api_key} onChange={(e) => setProviderDraft({ ...providerDraft, api_key: e.target.value })} /></label>
          <Button type="submit" disabled={saving || !providerDraft.api_key.trim()}><Plus />添加供应商</Button>
        </form>
        {data.providers.length > 0 && <form id="ai-model-form" className="ai-form ai-add-model" onSubmit={(event) => void saveModel(event)}>
          <h4>{editingModel ? "编辑模型" : "手动添加模型"}</h4>
          {!editingModel && <label>所属供应商<Select required value={modelProvider} onChange={(e) => setModelProvider(e.target.value)}><option value="">选择供应商</option>{data.providers.map((p) => <option value={p.id} key={p.id}>{p.name}</option>)}</Select></label>}
          <label>模型 ID / 名称<Input required maxLength={200} value={modelDraft.model_name} onChange={(e) => setModelDraft({ ...modelDraft, model_name: e.target.value })} /></label>
          <label>上下文窗口（tokens）<Input required type="number" min={1} max={10000000} value={modelDraft.context_window} onChange={(e) => setModelDraft({ ...modelDraft, context_window: Number(e.target.value) })} /></label>
          <label>最大输出（tokens）<Input required type="number" min={1} max={1000000} value={modelDraft.max_output_tokens} onChange={(e) => setModelDraft({ ...modelDraft, max_output_tokens: Number(e.target.value) })} /></label>
          <fieldset className="ai-input-types"><legend>输入类型</legend>{(["text", "image"] as const).map((type) => <label key={type}><input type="checkbox" checked={modelDraft.input_types.includes(type)} onChange={(e) => setModelDraft({ ...modelDraft, input_types: e.target.checked ? [...modelDraft.input_types, type] : modelDraft.input_types.filter((item) => item !== type) })} />{type === "text" ? "文本" : "图像"}</label>)}</fieldset>
          <div className="ai-reasoning">
            <label>
              <input
                type="checkbox"
                checked={modelDraft.reasoning.supported}
                onChange={(event) => {
                  const supported = event.target.checked;
                  const levels = supported
                    ? modelDraft.reasoning.levels.length
                      ? modelDraft.reasoning.levels
                      : DEFAULT_REASONING_LEVELS
                    : [];
                  setModelDraft({
                    ...modelDraft,
                    reasoning: {
                      ...modelDraft.reasoning,
                      supported,
                      levels,
                      default_level: supported ? modelDraft.reasoning.default_level || null : null,
                    },
                  });
                }}
              />
              支持思考强度
            </label>
            {modelDraft.reasoning.supported && <><label>API 参数<Select value={modelDraft.reasoning.parameter || "reasoning_effort"} onChange={(e) => setModelDraft({ ...modelDraft, reasoning: { ...modelDraft.reasoning, parameter: e.target.value } })}><option value="reasoning_effort">reasoning_effort</option><option value="reasoning.effort">reasoning.effort</option></Select></label>
               <label>思考档位<ReasoningLevelPicker levels={modelDraft.reasoning.levels} onChange={(levels) => setModelDraft({ ...modelDraft, reasoning: { ...modelDraft.reasoning, levels, default_level: modelDraft.reasoning.default_level && levels.some((level) => level.label === modelDraft.reasoning.default_level) ? modelDraft.reasoning.default_level : null } })} /></label>
               {modelDraft.reasoning.levels.length > 0 && <label>默认思考强度<Select aria-label="默认思考强度" value={modelDraft.reasoning.default_level || ""} onChange={(e) => setModelDraft({ ...modelDraft, reasoning: { ...modelDraft.reasoning, default_level: e.target.value || null } })}><option value="">自动</option>{modelDraft.reasoning.levels.filter((level) => REASONING_LEVELS.includes(level.api_value as (typeof REASONING_LEVELS)[number])).map((level) => <option key={`${level.label}-${level.api_value}`} value={level.label}>{reasoningLevelLabel(level)}</option>)}</Select></label>}</>}
          </div>
          <p className="ai-capability-note">以上能力会随账户默认模型用于后端请求；不支持的思考能力不会发送对应参数。上下文大小用于请求前校验（仅在可估算时）。</p>
          {modelDraft.max_output_tokens > modelDraft.context_window && <p className="ai-error" role="alert">最大输出不能大于上下文窗口。</p>}
          {modelDraft.input_types.length === 0 && <p className="ai-error" role="alert">至少选择一种输入类型。</p>}
          <div className="settings-actions"><Button type="submit" disabled={saving || !modelProvider || !modelDraft.model_name.trim() || modelDraft.max_output_tokens > modelDraft.context_window || modelDraft.input_types.length === 0 || (modelDraft.reasoning.supported && !modelDraft.reasoning.levels.length)}><Save />{editingModel ? "保存模型" : "添加模型"}</Button>{editingModel && <Button type="button" variant="secondary" onClick={() => { setEditingModel(null); setModelDraft(blankModel()); }}>取消编辑</Button>}</div>
        </form>}
        <div className="ai-default"><label htmlFor="ai-default-model">默认模型</label><Select id="ai-default-model" value={data.default_model_id || ""} disabled={saving || models.length === 0} onChange={(e) => void setDefault(e.target.value)}><option value="">选择默认模型</option>{models.filter(({ provider, model }) => provider.enabled && !model.needs_manual_capabilities).map(({ provider, model }) => <option key={model.id} value={model.id}>{provider.name} / {model.model_name}</option>)}</Select>{models.length === 0 && <small>添加模型后可选择默认模型。</small>}</div>

      </>}
    </div>
  </div>;
}
