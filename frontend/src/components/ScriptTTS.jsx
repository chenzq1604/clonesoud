/**
 * TTS 文本输入组件
 *
 * 输入待合成的文本，从音色列表（内置音色 + 克隆音色分组）中选择音色，
 * 并选择语速（0.5x ~ 2.0x），调用后端生成对应音色的音频；
 * 内置音色可试听约 2 秒示例语音，克隆音色可试听克隆时的原始录音。
 * 每次生成自动存入语音库，可试听、选用（作为本项目语音）与删除。
 */

import { useEffect, useRef, useState } from "react";
import { API_BASE, api } from "../api";
import ProgressIndicator from "./ProgressIndicator";

/** 可选语速档位（值与后端 CosyVoice speed 系数一致） */
const SPEED_OPTIONS = [
  { value: 0.5, label: "0.5x 慢速" },
  { value: 0.7, label: "0.7x 较慢" },
  { value: 1.0, label: "1.0x 正常" },
  { value: 1.2, label: "1.2x 稍快" },
  { value: 1.25, label: "1.25x 略快" },
  { value: 1.5, label: "1.5x 较快" },
  { value: 2.0, label: "2.0x 快速" },
];

function ScriptTTS({ projectId, project, onStatusChange }) {
  const [text, setText] = useState(project?.tts_text || "");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [voices, setVoices] = useState([]);
  const [selectedSpeaker, setSelectedSpeaker] = useState("");
  const [previewingSpeaker, setPreviewingSpeaker] = useState("");
  const [speed, setSpeed] = useState(1.0);
  const [library, setLibrary] = useState([]);
  const [operatingItemId, setOperatingItemId] = useState("");

  const previewAudioRef = useRef(null);

  const ttsStatus = project?.tts_status || "pending";
  const isGenerating = ttsStatus === "generating" || loading;
  const isReady = ttsStatus === "ready";
  const isFailed = ttsStatus === "failed";

  /** 内置音色与克隆音色分组 */
  const builtinVoices = voices.filter((v) => v.is_builtin);
  const cloneVoices = voices.filter((v) => !v.is_builtin);

  /** 拉取音色列表（音色状态变化时刷新，以便新克隆音色出现） */
  const loadVoices = async (signal) => {
    try {
      const list = await api.getVoices();
      if (signal?.cancelled) return;
      setVoices(list);
    } catch (err) {
      if (signal?.cancelled) return;
      setMessage(`获取音色列表失败: ${err.message}`);
    }
  };

  /** 拉取语音库列表 */
  const loadLibrary = async (signal) => {
    try {
      const list = await api.getTTSLibrary();
      if (signal?.cancelled) return;
      setLibrary(list);
    } catch (err) {
      if (signal?.cancelled) return;
      setMessage(`获取语音库失败: ${err.message}`);
    }
  };

  /** 首次挂载与项目音色状态变化时刷新列表（带卸载取消守卫） */
  useEffect(() => {
    const signal = { cancelled: false };
    loadVoices(signal);
    return () => {
      signal.cancelled = true;
    };
  }, [project?.voice_status, project?.speaker_id]);

  /** 首次挂载时加载语音库（带卸载取消守卫） */
  useEffect(() => {
    const signal = { cancelled: false };
    loadLibrary(signal);
    return () => {
      signal.cancelled = true;
    };
  }, []);

  /** 页面刷新后回填后端保存的文案（组件挂载时 project 可能尚未加载完成） */
  useEffect(() => {
    if (project?.tts_text && !text) {
      setText(project.tts_text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.tts_text]);

  /** 默认选中当前项目绑定的音色 */
  useEffect(() => {
    if (project?.voice_status === "ready" && project.speaker_id) {
      setSelectedSpeaker(project.speaker_id);
    }
  }, [project?.speaker_id, project?.voice_status]);

  /** 未绑定音色时默认选中第一个内置音色，避免下拉框显示值与受控状态不一致 */
  useEffect(() => {
    if (selectedSpeaker) return;
    if (project?.voice_status === "ready" && project.speaker_id) return;
    if (voices.length === 0) return;
    const fallback = voices.find((v) => v.is_builtin) || voices[0];
    if (fallback) setSelectedSpeaker(fallback.speaker_id);
  }, [voices, selectedSpeaker, project?.voice_status, project?.speaker_id]);

  /** 根据选中的音色找到对应的试听地址（内置音色为示例语音，克隆音色为原始录音） */
  const selectedVoice = voices.find((v) => v.speaker_id === selectedSpeaker);
  const rawRecordingUrl = selectedVoice?.raw_recording_url
    ? `${API_BASE}${selectedVoice.raw_recording_url}`
    : "";

  /** 格式化音色创建时间，用于区分同名音色 */
  const formatVoiceTime = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  /** 当前是否正在试听所选音色（未选中音色时不视为播放中） */
  const isPreviewing = selectedSpeaker !== "" && previewingSpeaker === selectedSpeaker;

  /** 试听/停止试听所选音色（内置音色为示例语音，克隆音色为原始录音） */
  const togglePreview = () => {
    if (!rawRecordingUrl) return;
    const audio = previewAudioRef.current;
    if (!audio) return;
    if (isPreviewing && !audio.paused) {
      audio.pause();
    } else {
      audio.currentTime = 0;
      audio.play().catch((err) => setMessage(`试听失败: ${err.message}`));
    }
  };

  /** 切换音色时停止当前试听 */
  const handleVoiceChange = (speakerId) => {
    setSelectedSpeaker(speakerId);
    const audio = previewAudioRef.current;
    if (audio && !audio.paused) {
      audio.pause();
    }
  };

  const handlePreviewEvents = {
    onPlay: () => setPreviewingSpeaker(selectedSpeaker),
    onPause: () => setPreviewingSpeaker(""),
    onEnded: () => setPreviewingSpeaker(""),
  };

  /** 试听按钮提示文案（区分内置音色与克隆音色） */
  const previewTitle = selectedVoice?.is_builtin
    ? rawRecordingUrl
      ? "试听该内置音色的示例语音（约 2 秒）"
      : "内置音色试听音频缺失，可直接生成语音试听效果"
    : rawRecordingUrl
      ? "试听该音色克隆时的原始录音"
      : "该音色的原音文件缺失，无法试听";

  /** 提交文本进行 TTS 合成（带所选语速，成功后自动存入语音库） */
  const handleGenerate = async () => {
    if (loading) return; // 函数级守卫：按钮 disabled 有重渲染窗口
    if (!text.trim()) {
      setMessage("请输入待合成的文本");
      return;
    }
    if (!selectedSpeaker) {
      setMessage("请先选择音色（内置音色或克隆音色）");
      return;
    }

    setLoading(true);
    setMessage("正在合成语音...");
    try {
      const data = await api.generateTTS({
        project_id: projectId,
        text,
        speaker_id: selectedSpeaker,
        speed,
      });
      setMessage("语音合成完成，已存入语音库");
      if (onStatusChange) onStatusChange(data);
      loadLibrary();
    } catch (err) {
      setMessage(`合成失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  /** 选用语音库条目作为本项目的合成语音（供第 5 步合并使用） */
  const handleUse = async (item) => {
    setOperatingItemId(item.id);
    try {
      const data = await api.useTTSItem(item.id, projectId);
      setMessage(`已选用语音库音频（${item.speaker_name || "未知音色"} · ${item.speed}x）`);
      if (onStatusChange) onStatusChange(data);
    } catch (err) {
      setMessage(`选用失败: ${err.message}`);
    } finally {
      setOperatingItemId("");
    }
  };

  /** 删除语音库条目（二次确认，避免误删） */
  const handleDelete = async (item) => {
    const excerpt = (item.text || "").slice(0, 20);
    if (!window.confirm(`确定删除该条语音吗？\n「${excerpt}${(item.text || "").length > 20 ? "…" : ""}」`)) {
      return;
    }
    setOperatingItemId(item.id);
    try {
      await api.deleteTTSItem(item.id);
      setMessage("语音库条目已删除");
      loadLibrary();
    } catch (err) {
      setMessage(`删除失败: ${err.message}`);
    } finally {
      setOperatingItemId("");
    }
  };

  /** 估算合成耗时（秒）：长文本按前端规则切分后逐段合成，实测约 0.45 秒/字 */
  const estimatedSeconds = Math.max(30, Math.round(text.length * 0.45));

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-step-num">2</div>
        <h2>输入脚本并生成语音</h2>
      </div>

      <div className="form-group">
        <label>选择音色</label>
        <div className="voice-select-row">
          <select
            value={selectedSpeaker}
            onChange={(e) => handleVoiceChange(e.target.value)}
            disabled={isGenerating || voices.length === 0}
          >
            {voices.length === 0 && <option value="">暂无可用音色</option>}
            {builtinVoices.length > 0 && (
              <optgroup label="内置音色">
                {builtinVoices.map((v) => (
                  <option key={v.speaker_id} value={v.speaker_id}>
                    {v.speaker_name}
                  </option>
                ))}
              </optgroup>
            )}
            {cloneVoices.length > 0 && (
              <optgroup label="我的克隆音色">
                {cloneVoices.map((v) => (
                  <option key={v.speaker_id} value={v.speaker_id}>
                    {v.speaker_name}（{formatVoiceTime(v.created_at)}）
                    {v.project_id === projectId ? "〔本项目〕" : ""}
                    {v.raw_recording_url ? "" : "〔原音缺失〕"}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button
            onClick={togglePreview}
            disabled={!rawRecordingUrl || isGenerating}
            className="btn-secondary"
            title={previewTitle}
          >
            {isPreviewing ? "停止试听原音" : "试听原音"}
          </button>
          {/* 隐藏的原音播放器，由试听按钮控制；无地址时不设置 src 避免空请求 */}
          <audio
            ref={previewAudioRef}
            src={rawRecordingUrl || undefined}
            preload="none"
            {...handlePreviewEvents}
          />
        </div>
      </div>

      <div className="form-group">
        <label>语速</label>
        <select
          value={speed}
          onChange={(e) => setSpeed(parseFloat(e.target.value))}
          disabled={isGenerating}
        >
          {SPEED_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      <textarea
        rows={5}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="输入想让所选音色朗读的文本..."
      />
      <div className="form-actions">
        <button onClick={handleGenerate} disabled={isGenerating} className="btn-primary">
          {isGenerating && <span className="loading-spinner" />}
          {isGenerating ? "合成中..." : "生成语音"}
        </button>
      </div>
      <ProgressIndicator
        active={isGenerating}
        label="正在合成语音（本地 GPU 推理，长文本会自动分段合成，耗时随文本长度增加）"
        estimatedSeconds={estimatedSeconds}
      />
      {message && <p className="message">{message}</p>}

      {isReady && project?.tts_path && (
        <div className="result-info">
          <strong>语音已就绪</strong>
          <audio src={`${API_BASE}${project.tts_path}`} controls />
        </div>
      )}

      {isFailed && project?.tts_error && (
        <div className="error-detail">
          <strong>语音合成失败</strong>
          <pre>{project.tts_error}</pre>
        </div>
      )}

      <div className="tts-library">
        <div className="tts-library-header">
          <strong>语音库</strong>
          <span className="tts-library-count">{library.length} 条</span>
        </div>
        {library.length === 0 && (
          <p className="tts-library-empty">暂无生成记录，每次生成语音后会自动保存到语音库</p>
        )}
        {library.map((item) => (
          <div className="library-item" key={item.id}>
            {item.url ? (
              <audio src={`${API_BASE}${item.url}`} controls preload="none" />
            ) : (
              <span className="library-item-missing" title="音频文件缺失">文件缺失</span>
            )}
            <div className="library-item-info">
              <div className="library-item-text" title={item.text}>
                {item.text}
              </div>
              <div className="library-item-meta">
                {item.speaker_name || "未知音色"} · {item.speed}x ·{" "}
                {formatVoiceTime(item.created_at)}
              </div>
            </div>
            <div className="library-item-actions">
              <button
                onClick={() => handleUse(item)}
                disabled={operatingItemId === item.id || isGenerating || !item.url}
                className="btn-secondary"
                title="将该语音选用为本项目的合成语音（供第 5 步合并使用）"
              >
                选用
              </button>
              <button
                onClick={() => handleDelete(item)}
                disabled={operatingItemId === item.id}
                className="btn-danger"
              >
                删除
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default ScriptTTS;
