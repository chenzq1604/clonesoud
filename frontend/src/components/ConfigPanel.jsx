/**
 * 系统配置面板
 *
 * 以弹窗形式编辑运行参数（大模型 API Key、服务地址、超时、演示模式等）。
 * API Key 为敏感项：后端仅返回掩码，输入框留空表示保持不变。
 */

import { useEffect, useState } from "react";
import { api } from "../api";

function ConfigPanel({ open, onClose }) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [keyHint, setKeyHint] = useState("");

  // 表单状态（与后端 ConfigUpdate 字段一一对应）
  const [form, setForm] = useState({
    ark_api_key: "",
    image_model: "",
    http_proxy: "",
    comfyui_base_url: "",
    comfyui_timeout: 1800,
    cosyvoice_base_url: "",
    cosyvoice_timeout: 1800,
    demo_mode: false,
  });

  /** 打开面板时加载当前配置 */
  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setMessage("");
      setError("");
      try {
        const data = await api.getConfig();
        if (cancelled) return;
        setForm({
          ark_api_key: "",
          image_model: data.image_model || "",
          http_proxy: data.http_proxy || "",
          comfyui_base_url: data.comfyui_base_url || "",
          comfyui_timeout: data.comfyui_timeout ?? 1800,
          cosyvoice_base_url: data.cosyvoice_base_url || "",
          cosyvoice_timeout: data.cosyvoice_timeout ?? 1800,
          demo_mode: Boolean(data.demo_mode),
        });
        setKeyHint(
          data.ark_api_key_set
            ? `已配置（${data.ark_api_key_masked}），留空保持不变`
            : "未配置"
        );
      } catch (err) {
        if (!cancelled) setError(`加载配置失败: ${err.message}`);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [open]);

  /** 打开状态下按 ESC 关闭 */
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  /** 更新表单字段 */
  const setField = (field, value) => setForm((f) => ({ ...f, [field]: value }));

  /** 保存配置：密钥留空不提交；URL 类字段始终提交当前值（空串 = 清除） */
  const handleSave = async () => {
    if (saving) return;
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const body = {};
      if (form.ark_api_key.trim()) body.ark_api_key = form.ark_api_key.trim();
      // image_model 后端约束非空（min_length=1），留空时不提交
      if (form.image_model.trim()) body.image_model = form.image_model.trim();
      // URL 类字段始终提交（含空串）：后端支持空串清除配置，
      // 若仅在非空时提交，用户清空代理/服务地址后保存不生效
      body.http_proxy = form.http_proxy.trim();
      body.comfyui_base_url = form.comfyui_base_url.trim();
      body.cosyvoice_base_url = form.cosyvoice_base_url.trim();
      if (form.comfyui_timeout) body.comfyui_timeout = Number(form.comfyui_timeout);
      if (form.cosyvoice_timeout) body.cosyvoice_timeout = Number(form.cosyvoice_timeout);
      body.demo_mode = form.demo_mode;

      const data = await api.updateConfig(body);
      setMessage(data.message || "配置已保存");
      setForm((f) => ({ ...f, ark_api_key: "" }));
      // 保存后刷新掩码提示
      try {
        const cfg = await api.getConfig();
        setKeyHint(
          cfg.ark_api_key_set
            ? `已配置（${cfg.ark_api_key_masked}），留空保持不变`
            : "未配置"
        );
      } catch {
        /* 掩码刷新失败不影响主流程 */
      }
    } catch (err) {
      setError(`保存失败: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="config-overlay" onClick={onClose}>
      <div className="config-modal" onClick={(e) => e.stopPropagation()}>
        <div className="config-header">
          <h2>系统配置</h2>
          <button className="config-close" onClick={onClose} title="关闭">
            ×
          </button>
        </div>

        {loading ? (
          <p className="message">正在加载配置...</p>
        ) : (
          <>
            <section className="config-section">
              <h3>文生图（火山引擎方舟）</h3>
              <div className="form-group">
                <label>API Key</label>
                <input
                  type="password"
                  value={form.ark_api_key}
                  onChange={(e) => setField("ark_api_key", e.target.value)}
                  placeholder={keyHint}
                  autoComplete="new-password"
                />
                <p className="config-hint">{keyHint}；完整密钥仅保存在本机 .env 文件中</p>
              </div>
              <div className="form-group">
                <label>模型 ID</label>
                <input
                  type="text"
                  value={form.image_model}
                  onChange={(e) => setField("image_model", e.target.value)}
                />
              </div>
              <div className="form-group">
                <label>本地代理（访问火山引擎）</label>
                <input
                  type="text"
                  value={form.http_proxy}
                  onChange={(e) => setField("http_proxy", e.target.value)}
                  placeholder="http://127.0.0.1:7890"
                />
              </div>
            </section>

            <section className="config-section">
              <h3>视频生成（本地 ComfyUI）</h3>
              <div className="form-group">
                <label>服务地址</label>
                <input
                  type="text"
                  value={form.comfyui_base_url}
                  onChange={(e) => setField("comfyui_base_url", e.target.value)}
                />
              </div>
              <div className="form-group">
                <label>单任务超时（秒，10 ~ 7200）</label>
                <input
                  type="number"
                  min={10}
                  max={7200}
                  value={form.comfyui_timeout}
                  onChange={(e) => setField("comfyui_timeout", e.target.value)}
                />
              </div>
            </section>

            <section className="config-section">
              <h3>语音合成（本地 CosyVoice）</h3>
              <div className="form-group">
                <label>服务地址</label>
                <input
                  type="text"
                  value={form.cosyvoice_base_url}
                  onChange={(e) => setField("cosyvoice_base_url", e.target.value)}
                />
              </div>
              <div className="form-group">
                <label>单次合成超时（秒，10 ~ 7200）</label>
                <input
                  type="number"
                  min={10}
                  max={7200}
                  value={form.cosyvoice_timeout}
                  onChange={(e) => setField("cosyvoice_timeout", e.target.value)}
                />
              </div>
            </section>

            <section className="config-section">
              <h3>其他</h3>
              <label className="config-checkbox">
                <input
                  type="checkbox"
                  checked={form.demo_mode}
                  onChange={(e) => setField("demo_mode", e.target.checked)}
                />
                演示模式（文生图返回本地占位图，不调用外部 API）
              </label>
            </section>

            {message && <p className="message">{message}</p>}
            {error && <p className="message error">{error}</p>}

            <div className="form-actions">
              <button onClick={onClose} className="btn-secondary" disabled={saving}>
                关闭
              </button>
              <button onClick={handleSave} className="btn-primary" disabled={saving}>
                {saving && <span className="loading-spinner" />}
                {saving ? "保存中..." : "保存配置"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default ConfigPanel;
