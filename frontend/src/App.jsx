/**
 * 应用主组件
 *
 * 管理当前项目状态，展示步骤进度条、状态面板和 5 个功能卡片。
 */

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import ConfigPanel from "./components/ConfigPanel";
import HelpTip from "./components/HelpTip";
import ImagePrompt from "./components/ImagePrompt";
import MergePanel from "./components/MergePanel";
import Recorder from "./components/Recorder";
import ScriptTTS from "./components/ScriptTTS";
import VideoGen from "./components/VideoGen";
import "./App.css";

/** 步骤定义：顺序对应流程阶段 */
const STEPS = [
  { num: 1, label: "声音克隆", field: "voice_status" },
  { num: 2, label: "语音合成", field: "tts_status" },
  { num: 3, label: "文生图", field: "image_status" },
  { num: 4, label: "视频", field: "video_status" },
  { num: 5, label: "合并预览", field: "merge_status" },
];

/** 当前项目 ID 在 localStorage 中的持久化键：刷新页面后恢复同一项目，
 *  避免已生成的语音/视频因刷新而"丢失" */
const PROJECT_STORAGE_KEY = "clonempeg_project_id";

function App() {
  const [projectId, setProjectId] = useState(
    () => localStorage.getItem(PROJECT_STORAGE_KEY) || ""
  );
  const [project, setProject] = useState(null);
  const [error, setError] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  // 系统配置面板（大模型 Key、服务地址等）是否打开
  const [configOpen, setConfigOpen] = useState(false);
  // 防止 React StrictMode 双调用 effect 时重复创建项目
  const initRef = useRef(false);

  /** 创建新项目（成功后持久化 ID） */
  const createProject = async () => {
    try {
      const data = await api.createProject("音视频克隆项目");
      localStorage.setItem(PROJECT_STORAGE_KEY, data.id);
      setProjectId(data.id);
      setProject(data);
      setError("");
    } catch (err) {
      setError(`创建项目失败: ${err.message}`);
    }
  };

  /** 用户主动开始新项目：确认后创建并切换（旧项目及其产物保留） */
  const handleNewProject = async () => {
    if (creatingNew) return;
    if (
      !window.confirm(
        "开始新项目？\n当前项目的进度与产物会保留（克隆音色、语音库不受影响），页面将切换到全新项目。"
      )
    ) {
      return;
    }
    setCreatingNew(true);
    try {
      await createProject();
    } finally {
      setCreatingNew(false);
    }
  };

  /** 轮询刷新项目状态 */
  const refreshProject = useCallback(async () => {
    if (!projectId) return;
    try {
      const data = await api.getProject(projectId);
      setProject(data);
    } catch (err) {
      if (err.status === 404) {
        // 当前项目已不存在（如后端数据库被重置/清理），
        // 自动新建项目恢复可用，避免后续操作持续报错
        setError("当前项目已失效，已自动创建新项目");
        createProject();
        return;
      }
      setError(`刷新状态失败: ${err.message}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    // StrictMode 下 effect 会执行两次，用 ref 守卫避免重复初始化
    if (initRef.current) return;
    initRef.current = true;

    // 恢复上次使用的项目；项目已被删除时新建
    const existing = localStorage.getItem(PROJECT_STORAGE_KEY);
    if (existing) {
      api
        .getProject(existing)
        .then((data) => {
          setProjectId(data.id);
          setProject(data);
        })
        .catch(() => {
          localStorage.removeItem(PROJECT_STORAGE_KEY);
          createProject();
        });
    } else {
      createProject();
    }
  }, []);

  useEffect(() => {
    const timer = setInterval(refreshProject, 3000);
    return () => clearInterval(timer);
  }, [refreshProject]);

  /** 判断步骤状态：pending/active/done */
  const getStepState = (field) => {
    const status = project?.[field] || "pending";
    if (status === "ready") return "done";
    if (status === "generating" || status === "cloning") return "active";
    return "pending";
  };

  /** 渲染状态面板单元格 */
  const renderStatusCell = (label, status) => (
    <div className="status-cell" key={label}>
      <span className="status-cell-label">{label}</span>
      <span className={`status-badge status-${status}`}>{status}</span>
    </div>
  );

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-text">
          <h1>音视频克隆生成系统</h1>
          <p>录制声音 → 生成语音 → 文生图 → 图生视频 → 合并预览</p>
        </div>
        <button
          onClick={handleNewProject}
          disabled={creatingNew || !projectId}
          className="btn-secondary header-new-project"
          title="保留当前项目进度，开始一个全新项目"
        >
          {creatingNew && <span className="loading-spinner" />}
          {creatingNew ? "创建中..." : "新建项目"}
        </button>
        <button
          onClick={() => setConfigOpen(true)}
          className="btn-secondary header-config"
          title="配置大模型 Key、服务地址等参数"
        >
          设置
        </button>
      </header>

      {/* 系统配置弹窗（大模型 Key / 服务地址 / 超时 / 演示模式） */}
      <ConfigPanel open={configOpen} onClose={() => setConfigOpen(false)} />

      {error && <div className="error-banner">{error}</div>}

      {project && (
        <div className="stepper">
          {STEPS.map((step, idx) => {
            const state = getStepState(step.field);
            return (
              <Fragment key={step.num}>
                <div className="step-indicator">
                  <div className={`step-circle ${state}`}>
                    {state === "done" ? "✓" : step.num}
                  </div>
                  <span className={`step-label ${state}`}>{step.label}</span>
                </div>
                {idx < STEPS.length - 1 && (
                  <div className={`step-line ${state === "done" ? "done" : ""}`} />
                )}
              </Fragment>
            );
          })}
          <HelpTip>
            <strong>步骤进度说明</strong>
            <span className="tip-line">
              <i className="tip-dot gray" />灰色圈（数字）：该步骤未开始或失败
            </span>
            <span className="tip-line">
              <i className="tip-dot purple" />紫色圈（数字）：该步骤正在进行
            </span>
            <span className="tip-line">
              <i className="tip-dot green" />绿色圈（✓）：该步骤已完成
            </span>
            <span className="tip-line">连线变绿：其左侧的步骤已完成</span>
            <span className="tip-line">状态每 3 秒自动刷新，与右侧「项目状态」一致</span>
          </HelpTip>
        </div>
      )}

      {project && (
        <aside className="status-panel">
          <h3>
            项目状态
            <HelpTip>
              <strong>状态徽章说明</strong>
              <span className="tip-line">
                <i className="tip-dot gray" />PENDING：未开始，等待操作
              </span>
              <span className="tip-line">
                <i className="tip-dot amber" />
                GENERATING / CLONING：正在处理中
              </span>
              <span className="tip-line">
                <i className="tip-dot green" />READY：已完成，产物就绪
              </span>
              <span className="tip-line">
                <i className="tip-dot red" />FAILED：失败，对应卡片中有错误详情
              </span>
              <span className="tip-line">「音色 PENDING」不阻塞流程：第 2 步选择音色后会自动就绪</span>
            </HelpTip>
          </h3>
          <div className="status-grid">
            {renderStatusCell("音色", project.voice_status)}
            {renderStatusCell("语音", project.tts_status)}
            {renderStatusCell("图片", project.image_status)}
            {renderStatusCell("视频", project.video_status)}
            {renderStatusCell("合并", project.merge_status)}
          </div>
        </aside>
      )}

      <main className="main-content">
        {projectId && (
          <Fragment key={projectId}>
            <Recorder projectId={projectId} project={project} onStatusChange={refreshProject} />
            <ScriptTTS projectId={projectId} project={project} onStatusChange={refreshProject} />
            <ImagePrompt projectId={projectId} project={project} onStatusChange={refreshProject} />
            <VideoGen projectId={projectId} project={project} onStatusChange={refreshProject} />
            <MergePanel projectId={projectId} project={project} onStatusChange={refreshProject} />
          </Fragment>
        )}
      </main>
    </div>
  );
}

export default App;
