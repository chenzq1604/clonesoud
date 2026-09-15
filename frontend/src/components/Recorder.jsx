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

  /** 停止录音 */
  const stopRecording = () => {
    if (!mediaRecorderRef.current) return;
    mediaRecorderRef.current.stop();
    setIsRecording(false);
    clearInterval(timerRef.current);
  };

  /** 开始录音：请求麦克风权限并启动 MediaRecorder */
  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      /** 音频可视化 */
      const audioContext = new AudioContext();
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
    if (audioChunksRef.current.length === 0) return;

    const blob = new Blob(audioChunksRef.current, { type: "audio/webm" });
    const file = new File([blob], "recording.webm", { type: "audio/webm" });

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

  /** 组件卸载时清理资源 */
  useEffect(() => {
    return () => {
      clearInterval(timerRef.current);
      cancelAnimationFrame(animationRef.current);
    };
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
