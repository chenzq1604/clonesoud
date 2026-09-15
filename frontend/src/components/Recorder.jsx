/**
 * 录音组件
 *
 * 使用浏览器 MediaRecorder API 录制 10-30 秒音频，
 * 支持实时音量和录制倒计时显示。
 */

import { useEffect, useRef, useState } from "react";
import { api } from "../api";

const MIN_SECONDS = 10;
const MAX_SECONDS = 30;

function Recorder({ projectId, project, onStatusChange }) {
  const [isRecording, setIsRecording] = useState(false);
  const [recordedSeconds, setRecordedSeconds] = useState(0);
  const [volume, setVolume] = useState(0);
  const [speakerName, setSpeakerName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [message, setMessage] = useState("");

  /** 从项目状态推断当前步骤是否可用 */
  const voiceStatus = project?.voice_status || "pending";
  const isCloning = voiceStatus === "cloning" || uploading;
  const isReady = voiceStatus === "ready";
  const isFailed = voiceStatus === "failed";

  const mediaRecorderRef = useRef(null);
  const audioChunksRef = useRef([]);
  const timerRef = useRef(null);
  const analyserRef = useRef(null);
  const animationRef = useRef(null);
  const audioContextRef = useRef(null);

  /** 停止录音（自动停止与手动停止可能几乎同时触发，二次 stop 会抛异常） */
  const stopRecording = () => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    recorder.stop();
    setIsRecording(false);
    clearInterval(timerRef.current);
  };

  /** 关闭音量可视化使用的 AudioContext（浏览器限制页面约 6 个实例） */
  const closeAudioContext = () => {
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    analyserRef.current = null;
  };

  /** 开始录音：请求麦克风权限并启动 MediaRecorder */
  const startRecording = async () => {
    try {
      // 防御：上次录音的 AudioContext 未正常关闭时先回收
      closeAudioContext();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      /** 音频可视化 */
      const audioContext = new AudioContext();
      audioContextRef.current = audioContext;
      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
        }
      };

      mediaRecorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        cancelAnimationFrame(animationRef.current);
        closeAudioContext();
      };

      mediaRecorder.start(100);
      setIsRecording(true);
      setRecordedSeconds(0);
      setMessage("");
      startTimer();
      visualizeVolume();
    } catch (err) {
      setMessage(`无法访问麦克风: ${err.message}`);
    }
  };

  /** 上传录制的音频到后端进行音色克隆 */
  const uploadRecording = async () => {
    if (uploading) return; // 函数级守卫：按钮 disabled 有重渲染窗口
    if (audioChunksRef.current.length === 0) return;

    // 使用录音器实际输出的 MIME（Firefox 为 ogg、Safari 为 mp4），
    // 硬编码 webm 会导致文件类型与真实字节不符
    const mimeType = mediaRecorderRef.current?.mimeType || "audio/webm";
    const ext = mimeType.includes("ogg") ? "ogg" : mimeType.includes("mp4") ? "m4a" : "webm";
    const blob = new Blob(audioChunksRef.current, { type: mimeType });
    const file = new File([blob], `recording.${ext}`, { type: mimeType });

    const formData = new FormData();
    formData.append("project_id", projectId);
    formData.append("speaker_name", speakerName || "我的音色");
    formData.append("language", "0");
    formData.append("audio", file);

    setUploading(true);
    setMessage("正在上传并克隆音色...");
    try {
      const data = await api.uploadVoice(formData);
      setMessage("音色克隆已提交，请在下方查看状态。");
      // 上传成功后清空本条录音：避免克隆完成后按钮仍可点击、
      // 同一份录音被重复提交并覆盖项目音色
      audioChunksRef.current = [];
      setRecordedSeconds(0);
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`上传失败: ${err.message}`);
    } finally {
      setUploading(false);
    }
  };

  /** 录制计时器 */
  const startTimer = () => {
    timerRef.current = setInterval(() => {
      setRecordedSeconds((prev) => prev + 1);
    }, 1000);
  };

  /** 实时音量可视化 */
  const visualizeVolume = () => {
    if (!analyserRef.current) return;
    const dataArray = new Uint8Array(analyserRef.current.frequencyBinCount);
    analyserRef.current.getByteFrequencyData(dataArray);
    const average = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;
    setVolume(average);
    animationRef.current = requestAnimationFrame(visualizeVolume);
  };

  /** 录制时长达到上限时自动停止 */
  useEffect(() => {
    if (isRecording && recordedSeconds >= MAX_SECONDS) {
      stopRecording();
    }
  }, [recordedSeconds, isRecording]);

  /** 组件卸载时清理资源（切换项目时组件会重挂载） */
  useEffect(() => {
    return () => {
      // 录音进行中卸载：停止录音器与麦克风轨道，避免麦克风持续占用
      const recorder = mediaRecorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
      }
      clearInterval(timerRef.current);
      cancelAnimationFrame(animationRef.current);
      closeAudioContext();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const canUpload = !isRecording && recordedSeconds >= MIN_SECONDS && recordedSeconds <= MAX_SECONDS;

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-step-num">1</div>
        <h2>录制并克隆声音</h2>
      </div>
      <p>请保持安静环境，朗读任意文字 10-30 秒。</p>

      <div className="form-group">
        <label>音色名称</label>
        <input
          type="text"
          value={speakerName}
          onChange={(e) => setSpeakerName(e.target.value)}
          placeholder="例如：小明"
        />
      </div>

      <div className="recorder-controls">
        {!isRecording ? (
          <button onClick={startRecording} disabled={isCloning} className="btn-primary">
            {isCloning && <span className="loading-spinner" />}
            {isCloning ? "克隆中..." : "开始录音"}
          </button>
        ) : (
          <button onClick={stopRecording} className="btn-danger">
            停止录音 (<span className="record-timer">{recordedSeconds}s</span>)
          </button>
        )}

        <button onClick={uploadRecording} disabled={!canUpload || isCloning} className="btn-secondary">
          提交克隆
        </button>
      </div>

      {isRecording && (
        <div className="volume-bar">
          <div className="volume-fill" style={{ width: `${Math.min(volume, 100)}%` }} />
        </div>
      )}

      {message && <p className="message">{message}</p>}

      {isReady && project?.speaker_id && (
        <div className="result-info">
          <strong>音色已就绪</strong>
          <span className="speaker-id">Speaker ID: {project.speaker_id}</span>
        </div>
      )}

      {isFailed && project?.voice_error && (
        <div className="error-detail">
          <strong>克隆失败</strong>
          <pre>{project.voice_error}</pre>
        </div>
      )}
    </div>
  );
}

export default Recorder;
