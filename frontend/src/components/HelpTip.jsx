/**
 * 帮助提示图标组件
 *
 * 在标题/区域旁显示一个小问号圆圈，悬停或键盘聚焦时
 * 弹出说明气泡，用于解释界面元素的状态规则等。
 */

/**
 * @param {Object} props
 * @param {React.ReactNode} props.children 气泡内的说明内容
 * @returns {JSX.Element} 带悬停说明的问号图标
 */
function HelpTip({ children }) {
  return (
    <span className="help-tip" tabIndex={0} role="button" aria-label="查看说明">
      <span className="help-tip-icon">?</span>
      <span className="help-tip-content">{children}</span>
    </span>
  );
}

export default HelpTip;
