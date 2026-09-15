/**
 * 后端 API 调用封装
 */

const API_BASE = "http://127.0.0.1:8000";

export { API_BASE };

/**
 * 为响应错误构造带 HTTP 状态码的 Error 对象
 * @param {Response} response - fetch 响应对象
 * @param {object} data - 已解析的响应 JSON（可能为空对象）
 * @returns {Error} 附带 status 属性的错误对象
 */
function responseError(response, data) {
  const err = new Error(data.detail || `请求失败: ${response.status}`);
  err.status = response.status;
  return err;
}

/**
 * 发送 JSON POST 请求
 * @param {string} path - API 路径
 * @param {object} body - 请求体
 * @returns {Promise<object>} 响应 JSON
 */
async function postJson(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw responseError(response, data);
  }
  return data;
}

/**
 * 发送 GET 请求
 * @param {string} path - API 路径
 * @returns {Promise<object>} 响应 JSON
 */
async function getJson(path) {
  const response = await fetch(`${API_BASE}${path}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw responseError(response, data);
  }
  return data;
}

/**
 * 发送 multipart/form-data POST 请求
 * @param {string} path - API 路径
 * @param {FormData} formData - 表单数据
 * @returns {Promise<object>} 响应 JSON
 */
async function postForm(path, formData) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: formData,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw responseError(response, data);
  }
  return data;
}

/**
 * 发送 DELETE 请求
 * @param {string} path - API 路径
 * @returns {Promise<object>} 响应 JSON
 */
async function deleteRequest(path) {
  const response = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw responseError(response, data);
  }
  return data;
}

/**
 * 发送 PUT 请求（JSON）
 * @param {string} path - API 路径
 * @param {object} body - 请求体
 * @returns {Promise<object>} 响应 JSON
 */
async function putJson(path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw responseError(response, data);
  }
  return data;
}

export const api = {
  /** 健康检查 */
  health: () => getJson("/health"),

  /** 创建项目 */
  createProject: (name) => postJson("/api/projects", { name }),

  /** 查询配置（API Key 仅返回掩码） */
  getConfig: () => getJson("/api/config"),

  /** 更新配置（未提供的字段保持不变） */
  updateConfig: (body) => putJson("/api/config", body),

  /** 获取项目详情 */
  getProject: (projectId) => getJson(`/api/projects/${projectId}`),

  /** 上传录音并克隆音色 */
  uploadVoice: (formData) => postForm("/api/voice/upload", formData),

  /** 获取可用音色列表（内置 + 克隆） */
  getVoices: () => getJson("/api/voice/voices"),

  /** 生成 TTS */
  generateTTS: (body) => postJson("/api/tts/generate", body),

  /** 获取语音库列表（全部生成过的语音） */
  getTTSLibrary: () => getJson("/api/tts/library"),

  /** 选用语音库条目作为指定项目的合成语音 */
  useTTSItem: (itemId, projectId) =>
    postJson(`/api/tts/library/${itemId}/use`, { project_id: projectId }),

  /** 删除语音库条目 */
  deleteTTSItem: (itemId) => deleteRequest(`/api/tts/library/${itemId}`),

  /** 文生图 */
  generateImage: (body) => postJson("/api/image/generate", body),

  /** 选择图片 */
  selectImage: (body) => postJson("/api/image/select", body),

  /** 文生视频 / 图生视频（本地 ComfyUI Wan 2.2） */
  generateVideo: (body) => postJson("/api/video/generate", body),

  /** 上传本地已有视频作为项目视频产物 */
  uploadVideo: (formData) => postForm("/api/video/upload", formData),

  /** 合并最终视频 */
  merge: (formData) => postForm("/api/merge", formData),

  /** 预览最终视频 URL */
  previewUrl: (projectId) => `${API_BASE}/api/merge/preview/${projectId}`,
};
