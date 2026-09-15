/**
 * 合并与预览组件
 *
 * 上传可选 BGM，调用后端合并视频与音频，并预览最终产物。
 */

import { useState } from "react";
import { API_BASE, api } from "../api";
import ProgressIndicator from "./ProgressIndicator";

function MergePanel({ projectId, project, onStatusChange }) {
  const [bgmFile, setBgmFile] = useState(null);
  const [bgmVolume, setBgmVolume] = useState(0.2);
  const [audioMode, setAudioMode] = useState("replace");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");

  const mergeStatus = project?.merge_status || "pending";
  const isGenerating = mergeStatus === "generating" || loading;
  const isReady = mergeStatus === "ready";
  const isFailed = mergeStatus === "failed";

  // 合并前置条件：语音与视频均已就绪（图片阶段仅生成视频时需要）
  const ttsReady = project?.tts_status === "ready";
  const videoReady = project?.video_status === "ready";
  const prerequisitesReady = ttsReady && videoReady;

  /** 提交合并请求；force 为 true 时对音频超长强制截断合并 */
  const handleMerge = async (force = false) => {
    if (loading) return; // 函数级守卫：按钮 disabled 有重渲染窗口
    if (!prerequisitesReady) {
      setMessage("语音或视频尚未就绪，请先完成前置步骤");
      return;
    }

    const formData = new FormData();
    formData.append("project_id", projectId);
    formData.append("bgm_volume", String(bgmVolume));
    formData.append("fade_in", "1");
    formData.append("fade_out", "2");
    formData.append("audio_mode", audioMode);
    formData.append("force_merge", String(force));
    if (bgmFile) {
      formData.append("bgm", bgmFile);
    }

    setLoading(true);
    setMessage("正在合并最终视频...");
    try {
      const data = await api.merge(formData);
      setMessage("合并完成");
      setPreviewUrl(`${api.previewUrl(projectId)}?t=${Date.now()}`);
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      // 音频超长被后端拦截：询问用户是否接受截断后强制合并
      if (!force && err.status === 400 && err.message.includes("超过视频时长")) {
        const confirmed = window.confirm(`${err.message}\n\n是否仍要合并（超出部分将被截断）？`);
        if (confirmed) {
          setLoading(false);
          await handleMerge(true);
        } else {
          setMessage(`已取消合并：${err.message}`);
        }
        return;
      }
      setMessage(`合并失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-step-num">5</div>
        <h2>合并与预览</h2>
      </div>

      <div className="form-group">
        <label>音频模式</label>
        <div className="audio-mode-options">
          <label className={`audio-mode-option ${audioMode === "replace" ? "active" : ""}`}>
            <input
              type="radio"
              name="audio_mode"
              value="replace"
              checked={audioMode === "replace"}
              onChange={() => setAudioMode("replace")}
            />
            <span>
              <strong>替换音频</strong>
              <small>丢弃视频原声，仅保留合成语音（及 BGM）</small>
            </span>
          </label>
          <label className={`audio-mode-option ${audioMode === "overlay" ? "active" : ""}`}>
            <input
              type="radio"
              name="audio_mode"
              value="overlay"
              checked={audioMode === "overlay"}
              onChange={() => setAudioMode("overlay")}
            />
            <span>
              <strong>叠加音频</strong>
              <small>保留视频原声，与合成语音混合（视频无原声时自动替换）</small>
            </span>
          </label>
        </div>
      </div>

      <div className="form-group">
        <label>BGM 文件（可选）</label>
        <input
          type="file"
          accept="audio/*"
          onChange={(e) => setBgmFile(e.target.files?.[0] || null)}
        />
      </div>

      <div className="form-group">
        <label>BGM 音量 ({Math.round(bgmVolume * 100)}%)</label>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={bgmVolume}
          onChange={(e) => setBgmVolume(Number(e.target.value))}
        />
      </div>

      <div className="merge-prerequisites">
        <div className={`prereq-item ${ttsReady ? "done" : ""}`}>
          {ttsReady ? "✓" : "○"} 语音（第 2 步）{ttsReady ? "已就绪" : "未就绪"}
        </div>
        <div className={`prereq-item ${videoReady ? "done" : ""}`}>
          {videoReady ? "✓" : "○"} 视频（第 4 步生成或上传本地视频）
          {videoReady ? "已就绪" : "未就绪"}
        </div>
      </div>

      <div className="form-actions">
        <button
          onClick={() => handleMerge(false)}
          disabled={isGenerating || !prerequisitesReady}
          className="btn-primary"
          title={prerequisitesReady ? "" : "语音或视频尚未就绪"}
        >
          {isGenerating && <span className="loading-spinner" />}
          {isGenerating ? "合并中..." : "生成最终视频"}
        </button>
      </div>

      <ProgressIndicator
        active={isGenerating}
        label="正在合并视频与音频（本地 FFmpeg）"
        estimatedSeconds={40}
      />

      {message && <p className="message">{message}</p>}

      {(previewUrl || (isReady && project?.final_video_path)) && (
        <div className="preview-area">
          <video
            src={previewUrl || `${API_BASE}${project.final_video_path}?t=${Date.now()}`}
            controls
          />
          <a
            href={previewUrl || `${API_BASE}${project.final_video_path}`}
            download
            className="btn-secondary"
          >
            下载视频
          </a>
        </div>
      )}

      {isFailed && project?.merge_error && (
        <div className="error-detail">
          <strong>合并失败</strong>
          <pre>{project.merge_error}</pre>
        </div>
      )}
    </div>
  );
}

export default MergePanel;
