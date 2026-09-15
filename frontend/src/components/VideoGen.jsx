/**
 * 视频组件（第 4 步）
 *
 * 提供三种互斥的视频来源方式：图生视频、文生视频（默认）、上传本地视频。
 * 选中某一方式后，才展示该方式对应的详细设置。
 */

import { useRef, useState } from "react";
import { api } from "../api";
import ProgressIndicator from "./ProgressIndicator";

/** 三种互斥的视频来源方式定义 */
const VIDEO_MODES = [
  { value: "text_to_video", label: "文生视频", desc: "直接根据文字描述生成视频" },
  { value: "image_to_video", label: "图生视频", desc: "基于第 3 步生成的图片做镜头运动" },
  { value: "upload", label: "上传视频", desc: "使用本地已生成好的视频" },
];

/** 分辨率档位（宽高均为 16 的倍数，满足 Wan VAE 下采样约束） */
const RES_OPTIONS = [
  { w: 832, h: 480, label: "832×480（最快）" },
  { w: 960, h: 544, label: "960×544" },
  { w: 1088, h: 624, label: "1088×624" },
  { w: 1216, h: 688, label: "1216×688" },
  { w: 1280, h: 704, label: "1280×704（默认）" },
];

// 与后端 comfyui_video.py 保持一致的约束
// 显存预算（像素·帧）：满载标定值打 85 折——121 帧满载会触发
// 驱动级挂起（GPU 卡死），打折后 1280×704 每段 101 帧稳定
const PIXEL_FRAME_BUDGET = Math.floor(1280 * 704 * 121 * 0.85);
const MAX_LENGTH = 169; // 单段总帧数硬上限
const MIN_LENGTH = 9;
const MAX_DURATION = 300; // 总时长上限（秒），超出单段预算自动分段

/**
 * 计算实际生成帧数（与后端 compute_video_length 逻辑一致：
 * 间隔帧数转换为 4k+1 形式，并按显存预算与硬上限钳制）。
 */
const computeFrameCount = (duration, fps, w, h) => {
  const interval = Math.max(1, Math.round(duration * fps));
  let length = 4 * Math.floor(interval / 4) + 1;
  const budget = Math.floor(PIXEL_FRAME_BUDGET / (w * h));
  length = Math.min(length, budget, MAX_LENGTH);
  length = 4 * Math.floor((Math.max(length, MIN_LENGTH) - 1) / 4) + 1;
  return Math.max(MIN_LENGTH, length);
};

function VideoGen({ projectId, project, onStatusChange }) {
  const [mode, setMode] = useState("text_to_video");
  const [prompt, setPrompt] = useState(project?.video_prompt || "");
  const [resIndex, setResIndex] = useState(RES_OPTIONS.length - 1);
  const [fps, setFps] = useState(24);
  const [duration, setDuration] = useState(5);
  const [loading, setLoading] = useState(false);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");

  const fileInputRef = useRef(null);

  // 当前分辨率档位与实际可生成的帧数 / 时长（时长按上限钳制，
  // 避免切换分辨率/帧率后残留超限的旧值）
  const res = RES_OPTIONS[resIndex];
  // 单段最大帧数（预算内）与对应时长；超出部分自动分段（尾帧续写 + 拼接）
  const segMaxFrames = computeFrameCount(999, fps, res.w, res.h);
  const segDuration = (segMaxFrames - 1) / fps;
  const effectiveDuration = Math.min(duration, MAX_DURATION);
  const segmentCount = Math.max(1, Math.ceil(effectiveDuration / segDuration));
  // 总帧数 = 前 N-1 个满段 + 最后一段（剩余时长对齐 4k+1）
  const lastSegFrames = computeFrameCount(
    effectiveDuration - (segmentCount - 1) * segDuration, fps, res.w, res.h
  );
  const totalFrames = (segmentCount - 1) * segMaxFrames + lastSegFrames;
  // 拼接后总时长：所有帧连续播放，= (总帧数 - 1) / fps
  const actualDuration = (totalFrames - 1) / fps;
  // 预计耗时：按分段数 x 单段耗时（默认规格实测约 6.7 秒/帧，分辨率线性缩放），
  // 另留 90 秒排队 / 尾帧提取 / 拼接余量
  const estimatedSeconds = Math.round(
    segmentCount *
      (segMaxFrames * 6.7 * ((res.w * res.h) / (1280 * 704))) +
      90
  );

  const videoStatus = project?.video_status || "pending";
  const isGenerating = videoStatus === "generating" || loading;
  const isReady = videoStatus === "ready";
  const isFailed = videoStatus === "failed";

  // 解析后端写入 video_task_id 的分段进度：
  // JSON 形式 {"stage","done","total","seg_progress","resumed"}，
  // 旧格式 "N/M"（仅段完成数）做兼容回退
  const videoProgress = (() => {
    const raw = project?.video_task_id;
    if (!raw) return null;
    if (raw.trim().startsWith("{")) {
      try {
        const p = JSON.parse(raw);
        if (p && typeof p.done === "number" && typeof p.total === "number") {
          return {
            stage: p.stage || "generating",
            done: p.done,
            total: p.total,
            segProgress: typeof p.seg_progress === "number" ? p.seg_progress : 0,
            resumed: typeof p.resumed === "number" ? p.resumed : 0,
          };
        }
      } catch {
        /* 解析失败走旧格式回退 */
      }
    }
    const m = String(raw).match(/^(\d+)\/(\d+)$/);
    if (m) {
      return { stage: "generating", done: Number(m[1]), total: Number(m[2]), segProgress: 0, resumed: 0 };
    }
    return null;
  })();

  // 生成阶段总进度：已完成段数 + 当前段内进度，占比到总段数
  const genPercent = videoProgress
    ? videoProgress.stage === "concat"
      ? 100
      : ((videoProgress.done + videoProgress.segProgress) / videoProgress.total) * 100
    : null;

  // 进度条文案：区分段内推理 / 段完成 / 拼接 / 断点续传
  const genLabel = (() => {
    if (!videoProgress) {
      return "正在生成视频（本地 ComfyUI Wan 2.2，含排队与 GPU 渲染）";
    }
    const { stage, done, total, segProgress, resumed } = videoProgress;
    const resumedPrefix = resumed > 0 ? `已从第 ${resumed + 1} 段断点续传 · ` : "";
    if (stage === "concat") {
      return `${resumedPrefix}正在无损拼接 ${total} 段视频（流复制，很快完成）`;
    }
    if (done >= total) {
      return `${resumedPrefix}全部分段完成，正在收尾...`;
    }
    const segPct = Math.round(segProgress * 100);
    return (
      `${resumedPrefix}第 ${done + 1}/${total} 段推理中` +
      (segPct > 0 ? `（本段采样进度 ${segPct}%）` : "") +
      `，总进度 ${done}/${total} 段`
    );
  })();

  // 图生视频模式是否需要先在第 3 步生成图片
  const hasImage = Boolean(project?.image_path);
  const isImageMode = mode === "image_to_video";

  /** 估算本地视频上传耗时（按文件大小粗估，单位秒） */
  const uploadEstimatedSeconds = uploadFile
    ? Math.max(15, Math.round((uploadFile.size / (1024 * 1024)) * 3))
    : 30;

  /** 提交文生视频 / 图生视频任务 */
  const handleGenerate = async () => {
    if (!prompt.trim()) {
      setMessage("请输入视频内容描述");
      return;
    }
    if (isImageMode && !hasImage) {
      setMessage("图生视频需要先在「文生图」步骤生成并选择一张图片");
      return;
    }

    setLoading(true);
    setMessage("正在提交视频生成任务...");
    try {
      const data = await api.generateVideo({
        project_id: projectId,
        prompt,
        mode,
        width: res.w,
        height: res.h,
        fps,
        duration: effectiveDuration,
      });
      setMessage(
        `视频生成任务已提交（${res.w}×${res.h} / ${fps}fps / 共 ${totalFrames} 帧 ≈ ${actualDuration.toFixed(1)} 秒` +
          `${segmentCount > 1 ? `，分 ${segmentCount} 段生成` : ""}），` +
          "本地 GPU 推理耗时较长，请稍后刷新项目状态查看进度。"
      );
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`提交失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  /** 上传本地已生成好的视频，作为项目视频产物 */
  const handleUpload = async () => {
    if (!uploadFile) {
      setMessage("请先选择一个本地视频文件");
      return;
    }

    const formData = new FormData();
    formData.append("project_id", projectId);
    formData.append("video", uploadFile);

    setUploading(true);
    setMessage("正在上传并处理本地视频（FFmpeg 封装/转码）...");
    try {
      const data = await api.uploadVideo(formData);
      setMessage("本地视频已就绪，可直接进入第 5 步合并。");
      // 上传成功后清空文件选择，避免误以为还需再次上传
      setUploadFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`上传失败: ${err.message}`);
    } finally {
      setUploading(false);
    }
  };

  /** 格式化文件大小展示 */
  const formatFileSize = (bytes) =>
    bytes >= 1024 * 1024
      ? `${(bytes / 1024 / 1024).toFixed(1)}MB`
      : `${Math.max(1, Math.round(bytes / 1024))}KB`;

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-step-num">4</div>
        <h2>视频</h2>
      </div>

      {/* 互斥模式选择（单选） */}
      <div className="video-mode-tabs">
        {VIDEO_MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            className={`video-mode-tab ${mode === m.value ? "active" : ""}`}
            onClick={() => setMode(m.value)}
          >
            <span className="video-mode-radio" />
            <span className="video-mode-text">
              <strong>{m.label}</strong>
              <small>{m.desc}</small>
            </span>
          </button>
        ))}
      </div>

      {/* 文生视频 / 图生视频 共用设置 */}
      {(mode === "text_to_video" || isImageMode) && (
        <div className="video-mode-body">
          {isImageMode && !hasImage && (
            <p className="message">请先在第 3 步「文生图」生成并选择一张图片，再使用图生视频。</p>
          )}
          <textarea
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={
              isImageMode
                ? "描述视频中的镜头运动和场景变化..."
                : "描述期望生成的视频画面、场景和动作..."
            }
          />

          {/* 生成参数滑块：分辨率 / 帧率 / 时长 */}
          <div className="slider-group">
            <div className="slider-row">
              <label>分辨率</label>
              <input
                type="range"
                min={0}
                max={RES_OPTIONS.length - 1}
                step={1}
                value={resIndex}
                disabled={isGenerating}
                onChange={(e) => setResIndex(Number(e.target.value))}
              />
              <span className="slider-value">{res.label}</span>
            </div>
            <div className="slider-row">
              <label>帧率</label>
              <input
                type="range"
                min={8}
                max={30}
                step={2}
                value={fps}
                disabled={isGenerating}
                onChange={(e) => setFps(Number(e.target.value))}
              />
              <span className="slider-value">{fps} fps</span>
            </div>
            <div className="slider-row">
              <label>时长</label>
              <input
                type="range"
                min={1}
                max={MAX_DURATION}
                step={0.5}
                value={effectiveDuration}
                disabled={isGenerating}
                onChange={(e) => setDuration(Number(e.target.value))}
              />
              <span className="slider-value">{effectiveDuration.toFixed(1)} 秒</span>
            </div>
            <p className="upload-file-hint">
              将生成 {res.w}×{res.h} / {fps}fps / 共 {totalFrames} 帧 ≈{" "}
              {actualDuration.toFixed(1)} 秒
              {segmentCount > 1 &&
                `（分 ${segmentCount} 段，每段最长 ${segDuration.toFixed(1)} 秒，段间画面自动续写）`}
              ，预计耗时约 {Math.round(estimatedSeconds / 60)} 分钟（RTX 2080 Ti 本地推理）
            </p>
            {effectiveDuration > 30 && (
              <p className="video-long-warning">
                {effectiveDuration >= 120
                  ? "长视频警告：此时长需要大量分段顺序推理，可能耗时数小时；中途失败支持断点续传" +
                    "（已完成段保留，重新生成时自动从失败段继续）。如已有等长视频（如外部工具生成），" +
                    "建议改用「上传视频」以节省时间。"
                  : "提示：超过 30 秒的视频将拆分为多段顺序生成，耗时随段数线性增加；失败后支持断点续传。"}
              </p>
            )}
          </div>

          <div className="form-actions">
            <button
              onClick={handleGenerate}
              disabled={isGenerating || uploading}
              className="btn-primary"
            >
              {isGenerating && <span className="loading-spinner" />}
              {isGenerating ? "生成中..." : "生成视频"}
            </button>
          </div>
          <ProgressIndicator
            active={isGenerating}
            label={genLabel}
            estimatedSeconds={estimatedSeconds}
            percent={genPercent}
          />
        </div>
      )}

      {/* 上传本地视频 专属设置 */}
      {mode === "upload" && (
        <div className="video-mode-body">
          <div className="form-group">
            <label>本地视频文件（支持 MP4 / MOV / AVI / WEBM / MKV，≤ 200MB）</label>
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
            />
            {uploadFile && (
              <p className="upload-file-hint">
                已选择：{uploadFile.name}（{formatFileSize(uploadFile.size)}），
                请点击下方「上传本地视频」按钮开始上传
              </p>
            )}
          </div>
          <div className="form-actions">
            <button
              onClick={handleUpload}
              disabled={uploading || isGenerating || !uploadFile}
              className="btn-secondary"
            >
              {uploading && <span className="loading-spinner" />}
              {uploading ? "上传处理中..." : "上传本地视频"}
            </button>
          </div>
          <ProgressIndicator
            active={uploading}
            label="正在上传并处理本地视频（FFmpeg 封装/转码）"
            estimatedSeconds={uploadEstimatedSeconds}
          />
        </div>
      )}

      {message && <p className="message">{message}</p>}

      {isReady && project?.video_path && (
        <div className="result-info">
          <strong>视频已就绪</strong>
          <video src={`http://127.0.0.1:8000${project.video_path}`} controls />
        </div>
      )}

      {isFailed && project?.video_error && (
        <div className="error-detail">
          <strong>视频生成失败</strong>
          <pre>{project.video_error}</pre>
          <p className="video-retry-hint">
            直接重新点击「生成视频」即可：参数不变时将自动从失败的分段继续（断点续传），
            已完成的分段不会重新推理。
          </p>
        </div>
      )}
    </div>
  );
}

export default VideoGen;