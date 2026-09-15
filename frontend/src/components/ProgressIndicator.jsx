/**
 * 不确定时长任务的进度指示器
 *
 * 后端各生成接口（语音/图片/视频/合并）为单一长请求时无法回报真实进度，
 * 采用"渐近式"进度条：进度随耗时增长并逐渐放缓（上限 90%），
 * 任务完成时父组件结束 active 状态即可；同时展示已耗时与预估时长，
 * 超出预估时长 1.5 倍后显示安抚提示。
 * 传入 percent（0~100）时优先展示后端回报的真实进度。
 */

import { useEffect, useState } from "react";

/**
 * @param {Object} props
 * @param {boolean} props.active 是否进行中（false 时组件不渲染）
 * @param {string} props.label 阶段文案，如"正在合成语音"
 * @param {number} props.estimatedSeconds 预估耗时（秒）
 * @param {number|null} props.percent 真实进度百分比（0~100），null 时用渐近式估算
 * @returns {JSX.Element|null} 进度指示器
 */
function ProgressIndicator({ active, label, estimatedSeconds = 30, percent = null }) {
  const [elapsed, setElapsed] = useState(0);

  /** active 期间每 500ms 更新已耗时，结束时归零 */
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const start = Date.now();
    const timer = setInterval(() => {
      setElapsed(Math.floor((Date.now() - start) / 1000));
    }, 500);
    return () => clearInterval(timer);
  }, [active]);

  if (!active) return null;

  // 真实进度优先；无真实进度时渐近式估算
  const hasRealPercent = percent !== null && percent !== undefined && !Number.isNaN(percent);
  const progress = hasRealPercent
    ? Math.min(100, Math.max(0, Math.round(percent)))
    : Math.min(
        90,
        Math.round(90 * (1 - Math.exp(-elapsed / (estimatedSeconds * 0.5))))
      );
  const overtime = !hasRealPercent && elapsed > estimatedSeconds * 1.5;

  return (
    <div className="progress-indicator">
      <div className="progress-header">
        <span className="progress-label">{label}</span>
        <span className="progress-time">
          {hasRealPercent
            ? `${progress}% · 已耗时 ${elapsed} 秒`
            : `已耗时 ${elapsed} 秒 / 预计约 ${estimatedSeconds} 秒`}
        </span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${progress}%` }} />
      </div>
      {overtime && (
        <p className="progress-hint">耗时较长，仍在处理中，请耐心等待…</p>
      )}
    </div>
  );
}

export default ProgressIndicator;
