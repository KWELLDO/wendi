# KaTeX（内置，勿手改）

- 版本：**0.18.9**（MIT，见 `LICENSE`）
- 来源：`https://registry.npmjs.org/katex/-/katex-<版本>.tgz` → `package/dist/`
- 内容：`katex.min.js`、`katex.min.css`、`fonts/*.woff2`
- 裁剪：CSS 里 `woff`/`ttf` 回退源已删除（现代浏览器只取 woff2，省 0.9MB）；字体只留 woff2。

升级（把 `<版本>` 换成目标版本后整段跑）：

```bash
V=0.18.9
cd /tmp && curl -sSLo katex.tgz https://registry.npmjs.org/katex/-/katex-$V.tgz \
  && rm -rf katex-$V && mkdir katex-$V && tar xzf katex.tgz -C katex-$V
cd katex-$V/package
cp dist/katex.min.js dist/katex.min.css LICENSE <本站>/web/vendor/katex/
cp dist/fonts/*.woff2 <本站>/web/vendor/katex/fonts/
cd <本站>/web/vendor/katex
sed -i -E 's/,url\(fonts\/([A-Za-z0-9_-]+)\.woff\) format\("woff"\),url\(fonts\/\1\.ttf\) format\("truetype"\)//g' katex.min.css
grep -c '\.woff)\|\.ttf)' katex.min.css   # 期望输出 0
```

引用方式见 `web/index.html` 顶部 `<head>`：CSS 同步、JS `defer`（启动代码在 `DOMContentLoaded` 里跑，
defer 脚本先于该事件执行，所以首次 render 时 `window.katex` 必然就绪）。
