# web/fonts/ — 本地自托管字体

> 守 VERA 铁律:**本地部署优先**。字体不走 Google Fonts 外链,自托管 woff2 放本目录。
> `index.html` 顶部 `@font-face` 已声明指向本目录,**你把两个 woff2 放进来即生效**;放之前 `--font/--mono` 自动回退系统栈(PingFang/微软雅黑等),功能与可读性不受影响。

## 需要的两个文件(免费可商用,OFL 许可)

| 文件名(必须精确) | 字体 | 字重 | 官方下载 |
|---|---|---|---|
| `SpaceGrotesk-Variable.woff2` | Space Grotesk(标题/品牌) | 300-700 可变 | https://fonts.google.com/specimen/Space+Grotesk |
| `JetBrainsMono-Variable.woff2` | JetBrains Mono(数字/代码) | 100-800 可变 | https://www.jetbrains.com/lp/mono/ |

> 都用 **Variable(可变字重)** 版本,一个文件覆盖所有字重,体积最小。
> Google Fonts 页面右上角 "Download family" 下载的是 TTF,需用 fonttools/`pyftsubset` 或在线工具(如 https://fontsquirrel.com/tools/webfont-generator )转成 woff2。
> JetBrains Mono 官网 "Download" 里的 `.zip` 含 `woff2` 目录,直接取 `JetBrainsMono[wght].woff2` 并重命名为 `JetBrainsMono-Variable.woff2`。

## 放好后

无需重启后端 —— 浏览器硬刷新(Ctrl+F5)即可看到新字体。`@font-face` 用 `font-display:swap`,首次加载期间先用系统栈,加载完无缝切换,不闪白。

## 不想自托管怎么办

把 `index.html` 里 `--font` / `--mono` 行的 `'Space Grotesk', ` 和 `'JetBrains Mono', ` 前缀删掉,回退成纯系统栈 —— 视觉会损失性格(AI 味回升),但零依赖、零文件。
