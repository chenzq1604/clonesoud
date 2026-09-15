/**
 * 文生图组件
 *
 * 输入提示词生成一组图片，并选择其中一张作为视频首帧。
 */

import { useEffect, useState } from "react";
import { API_BASE, api } from "../api";
import ProgressIndicator from "./ProgressIndicator";

/**
 * 图片尺寸选项。
 * 注意：doubao-seedream-5.0-lite 要求生成图片至少 3686400 像素（约 2K），
 * 因此所有选项均使用不低于该下限的分辨率。
 */
const SIZE_OPTIONS = [
  { value: "2048x2048", label: "1:1 方形 (2048x2048)" },
  { value: "2560x1440", label: "16:9 宽屏 (2560x1440)" },
  { value: "1440x2560", label: "9:16 竖屏 (1440x2560)" },
];

function ImagePrompt({ projectId, project, onStatusChange }) {
  const [prompt, setPrompt] = useState(project?.image_prompt || "");
  const [size, setSize] = useState("2560x1440");
  const [images, setImages] = useState([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  // 双击放大预览的图片 URL（null 表示关闭）
  const [previewUrl, setPreviewUrl] = useState(null);

  const imageStatus = project?.image_status || "pending";
  const isGenerating = imageStatus === "generating" || loading;
  const isReady = imageStatus === "ready";
  const isFailed = imageStatus === "failed";

  // 大图预览打开时支持按 ESC 关闭
  useEffect(() => {
    if (!previewUrl) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape") setPreviewUrl(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [previewUrl]);

  /** 提交提示词生成图片（已有图片时即为重新生成，旧候选图会被后端清理） */
  const handleGenerate = async () => {
    if (loading) return; // 函数级守卫：按钮 disabled 有重渲染窗口
    if (!prompt.trim()) {
      setMessage("请输入图片提示词");
      return;
    }

    setLoading(true);
    setMessage("正在生成图片（一次并发生成 6 张）...");
    try {
      const data = await api.generateImage({ project_id: projectId, prompt, size });
      setImages(data.image_urls || []);
      setSelectedIndex(0);
      setMessage(
        `图片生成完成（${(data.image_urls || []).length} 张），请选择一张；不满意可点击「重新生成」`
      );
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`生成失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  /** 确认选中的图片 */
  const handleSelect = async () => {
    if (loading) return; // 函数级守卫：按钮 disabled 有重渲染窗口
    if (images.length === 0) return;

    setLoading(true);
    setMessage("正在保存选择...");
    try {
      const data = await api.selectImage({
        project_id: projectId,
        selected_index: selectedIndex,
      });
      setMessage("图片选择完成");
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`选择失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-step-num">3</div>
        <h2>文生图</h2>
      </div>
      <textarea
        rows={3}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder="描述你想要的画面..."
      />

      <div className="form-group">
        <label>图片尺寸</label>
        <select value={size} onChange={(e) => setSize(e.target.value)}>
          {SIZE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      <div className="form-actions">
        <button onClick={handleGenerate} disabled={isGenerating} className="btn-primary">
          {isGenerating && <span className="loading-spinner" />}
          {isGenerating ? "生成中..." : "生成图片"}
        </button>
      </div>

      <ProgressIndicator
        active={isGenerating}
        label="正在生成图片（方舟 Seedream）"
        estimatedSeconds={25}
      />

      {images.length > 0 && (
        <div className="image-grid">
          {images.map((url, index) => (
            <img
              key={index}
              src={`${API_BASE}${url}`}
              alt={`生成图片 ${index + 1}`}
              className={selectedIndex === index ? "selected" : ""}
              onClick={() => setSelectedIndex(index)}
              onDoubleClick={() => setPreviewUrl(url)}
              title="单击选中，双击查看大图"
            />
          ))}
        </div>
      )}

      {images.length > 0 && (
        <p className="upload-file-hint">单击选中候选图，双击按原始比例查看大图</p>
      )}

      {/* 双击打开的大图预览（原始比例，点击遮罩或按 ESC 关闭） */}
      {previewUrl && (
        <div className="image-lightbox" onClick={() => setPreviewUrl(null)}>
          <img
            src={`${API_BASE}${previewUrl}`}
            alt="大图预览"
            onClick={(e) => e.stopPropagation()}
          />
          <span className="image-lightbox-close" onClick={() => setPreviewUrl(null)}>
            ×
          </span>
          <span className="image-lightbox-tip">点击空白处或按 ESC 关闭</span>
        </div>
      )}

      {images.length > 0 && (
        <div className="form-actions">
          <button onClick={handleSelect} disabled={isGenerating} className="btn-secondary">
            确认选择第 {selectedIndex + 1} 张
          </button>
          <button onClick={handleGenerate} disabled={isGenerating} className="btn-secondary">
            {isGenerating && <span className="loading-spinner" />}
            重新生成
          </button>
        </div>
      )}

      {message && <p className="message">{message}</p>}

      {isReady && project?.image_path && (
        <div className="result-info">
          <strong>图片已就绪</strong>
          <img src={`${API_BASE}${project.image_path}`} alt="选中的图片" />
        </div>
      )}

      {isFailed && project?.image_error && (
        <div className="error-detail">
          <strong>图片生成失败</strong>
          <pre>{project.image_error}</pre>
        </div>
      )}
    </div>
  );
}

export default ImagePrompt;
