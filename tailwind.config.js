/** Tailwind 打包配置。
 *
 * 只需要 content 一条：类名全部写在 index.html / player.html 里。扫描器是纯文本
 * 正则匹配，所以 JS 字符串里那几串完整类名（比如跟随按钮的 className）也能扫到
 * —— 前提是**整串出现在文件里**，别在 JS 里拼字符串。
 *
 * 改完 HTML 要重新生成 CSS，跑 `python build_site.py` 就会自动带上这一步
 * （它内部调 tools/tailwindcss.exe，因为这台机器上没有 npm）。
 */
module.exports = {
  content: ['./index.html', './player.html'],
};
