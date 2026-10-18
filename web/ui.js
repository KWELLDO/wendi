/* 问底 WenDi · 共享对话框 / 轻提示（window.wdiag）
   -------------------------------------------------------------------------------------
   冻结契约（各页按此调用，勿改名）：

     window.wdiag = {
       confirm(message, opts = {})   → Promise<boolean>          // 确定 true / 取消·Esc false
       prompt(message, opts = {})    → Promise<string|null>      // 取消 → null
       toast(message, opts = {})     → undefined                 // 自动消失
     }
     confirm.opts: { title?, okText?, cancelText?, danger?: boolean }
     prompt.opts:  { title?, defaultValue?, placeholder?, okText? }
     toast.opts:   { ms? (默认 2600), kind?: "info" | "warn" | "err" }

   实现要点：
     · 原生 <dialog> + showModal()：遮罩走 ::backdrop，焦点陷阱/最上层由浏览器负责，不自绘。
     · Esc：监听 cancel → preventDefault() → 与「取消」走同一条收尾逻辑。
     · Promise 只 resolve 一次：settled 闸门；submit / cancel / close / 遮罩点击 四条路径都收口到 finish()。
     · 焦点：打开前记住 document.activeElement；打开后聚焦输入框（prompt）或首个动作按钮；
            关闭后归还（即使对话框节点已从 DOM 移除）。
     · 重复调用：每次 confirm/prompt 建独立 <dialog>，互不串台；toast 复用同一容器、最多叠 3 条。
     · 退化：浏览器不支持 showModal 时 confirm → window.confirm、prompt → window.prompt；
            toast 仍用普通 div 显示（不需要 dialog 支持），连 DOM 都不可用时退回 console。
     · 主题：只吃 tokens.css 的 token（UI 见 /ui.css）；本文件不含任何色值。

   加载：/ui.css 在 /tokens.css 之后；<script src="/ui.js" defer></script>。
   本文件不依赖任何页面脚本，也不向页面注入标记 —— 对话框/提示都按需创建。
   ========================================================================== */
(function () {
  "use strict";

  if (typeof window === "undefined" || typeof document === "undefined") return;
  if (window.wdiag) return;                       // 重复引入：以先加载的一份为准

  var CAN_DIALOG = (function () {
    try { return typeof document.createElement("dialog").showModal === "function"; }
    catch (e) { return false; }
  })();
  var CAN_POPOVER = (function () {
    try { return typeof HTMLElement.prototype.showPopover === "function"; }
    catch (e) { return false; }
  })();

  var seq = 0;
  function mk(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function txt(v) { return v == null ? "" : String(v); }
  function focusNode(n) {
    if (!n || typeof n.focus !== "function") return;
    try { n.focus({ preventScroll: true }); }
    catch (e) { try { n.focus(); } catch (e2) { /* 聚焦不上就算了 */ } }
  }

  /* ------------------------------ 对话框骨架 ------------------------------ */
  function build(message, opts, withInput) {
    var o = opts || {};
    var id = "wdlg-" + (++seq);

    var dlg = mk("dialog", "wdlg");
    dlg.setAttribute("role", "dialog");
    dlg.setAttribute("aria-modal", "true");
    if (o.danger) dlg.setAttribute("data-danger", "1");

    var form = mk("form");
    form.setAttribute("method", "dialog");

    var h = mk("h3", "wdlg-t", o.title ? txt(o.title) : null);
    h.id = id + "-t";
    var p = mk("p", "wdlg-m", txt(message));
    p.id = id + "-m";
    // 无标题时用正文当可访问名（h3 仍在 DOM 里，只是 :empty 不占位）
    dlg.setAttribute("aria-labelledby", o.title ? h.id : p.id);

    form.appendChild(h);
    form.appendChild(p);

    var input = null;
    if (withInput) {
      input = mk("input", "wdlg-in");
      input.type = "text";
      input.value = o.defaultValue == null ? "" : txt(o.defaultValue);
      if (o.placeholder) input.placeholder = txt(o.placeholder);
      input.setAttribute("autocomplete", "off");
      input.setAttribute("spellcheck", "false");
      form.appendChild(input);
    }

    var act = mk("div", "wdlg-act");
    var cancel = mk("button", "wdlg-c", o.cancelText ? txt(o.cancelText) : "取消");
    cancel.type = "button";                     // 不用 submit：避免 Enter 隐式提交到"取消"
    cancel.value = "cancel";
    var ok = mk("button", "wdlg-k", o.okText ? txt(o.okText) : "确定");
    ok.type = "submit";                         // 表单里唯一的 submit → Enter = 确定
    ok.value = "ok";
    act.appendChild(cancel);
    act.appendChild(ok);

    form.appendChild(act);
    dlg.appendChild(form);

    return { dlg: dlg, form: form, input: input, ok: ok, cancel: cancel };
  }

  /* 打开并把四条收尾路径收口到 finish()；pick(ok, input) 决定 resolve 的值。 */
  function open(parts, opts, pick) {
    var dlg = parts.dlg, input = parts.input, o = opts || {};
    var prev = (document.activeElement && document.activeElement !== document.body)
      ? document.activeElement : null;
    var settled = false;

    return new Promise(function (resolve) {
      function finish(v) {
        if (settled) return;                    // ← Promise 单次 resolve 的闸门
        settled = true;
        try { if (dlg.open) dlg.close(); } catch (e) {}
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        // 焦点归还：延到下一拍，避开"移除节点把焦点丢给 body"
        setTimeout(function () { focusNode(prev); }, 0);
        resolve(v);
      }

      // 1) 正常关闭（submit 被浏览器处理 / 程序 close）—— 兜底路径
      dlg.addEventListener("close", function () {
        finish(pick(dlg.returnValue === "ok", input));
      });
      // 2) Esc：拦下默认关闭，统一收尾（保证 Promise 一定 resolve）
      dlg.addEventListener("cancel", function (e) {
        e.preventDefault();
        finish(pick(false, input));
      });
      // 3) 取消按钮（type=button，不触发 submit）
      parts.cancel.addEventListener("click", function () { finish(pick(false, input)); });
      // 4) 确定：拦下默认提交，按 value 收尾
      parts.form.addEventListener("submit", function (e) {
        e.preventDefault();
        finish(pick(true, input));
      });
      // 点遮罩（点在 <dialog> 自身）＝取消
      dlg.addEventListener("mousedown", function (e) {
        if (e.target === dlg) finish(pick(false, input));
      });

      document.body.appendChild(dlg);
      try { dlg.showModal(); }
      catch (e) {
        // 理论到不了（CAN_DIALOG 已判定），真到不了就让调用方拿安全值
        finish(pick(false, input));
        return;
      }
      // 焦点：prompt 进输入框；危险确认落在「取消」上（防误按 Enter 毁数据）；其余落「确定」
      var target = input || (o.danger ? parts.cancel : parts.ok);
      focusNode(target);
      if (input) { try { input.select(); } catch (e) {} }
    });
  }

  /* ------------------------------ 公开：confirm ------------------------------ */
  function confirmDialog(message, opts) {
    if (!CAN_DIALOG) {
      try { return Promise.resolve(!!window.confirm(txt(message))); }
      catch (e) { return Promise.resolve(false); }
    }
    var parts = build(message, opts, false);
    return open(parts, opts, function (ok) { return ok; });
  }

  /* ------------------------------ 公开：prompt ------------------------------ */
  function promptDialog(message, opts) {
    if (!CAN_DIALOG) {
      try {
        var d = opts && opts.defaultValue != null ? txt(opts.defaultValue) : "";
        var r = window.prompt(txt(message), d);
        return Promise.resolve(r == null ? null : r);
      } catch (e) { return Promise.resolve(null); }
    }
    var parts = build(message, opts, true);
    return open(parts, opts, function (ok, input) {
      return ok ? String(input.value) : null;
    });
  }

  /* ------------------------------ 公开：toast ------------------------------ */
  var host = null, items = [], TOAST_MAX = 3;

  function ensureHost() {
    if (!host) {
      host = mk("div", "wtoast");
      host.setAttribute("role", "status");
      host.setAttribute("aria-live", "polite");
      if (CAN_POPOVER) host.setAttribute("popover", "manual");
    }
    // 落点：有 popover 就进 top layer（能盖住原生 <dialog>）；
    // 没有 popover 时若有对话框开着，就挂进对话框里，别被遮罩压住。
    var target = document.body;
    if (!CAN_POPOVER) {
      var d = document.querySelector("dialog.wdlg[open]");
      if (d) target = d;
    }
    if (host.parentNode !== target) target.appendChild(host);
    if (CAN_POPOVER) {
      var shown = false;
      try { shown = host.matches(":popover-open"); } catch (e) {}
      if (!shown) { try { host.showPopover(); } catch (e) {} }
    }
    return host;
  }

  function drop(it) {
    if (!it || it._wgone) return;
    it._wgone = true;
    if (it._wtimer) clearTimeout(it._wtimer);
    var i = items.indexOf(it);
    if (i >= 0) items.splice(i, 1);
    it.classList.add("out");
    setTimeout(function () {
      if (it.parentNode) it.parentNode.removeChild(it);
      if (!items.length && host && CAN_POPOVER) {
        try { host.hidePopover(); } catch (e) {}
      }
    }, 220);
  }

  function toast(message, opts) {
    var o = opts || {};
    var kind = (o.kind === "warn" || o.kind === "err") ? o.kind : "info";
    var ms = (typeof o.ms === "number" && isFinite(o.ms) && o.ms > 0) ? o.ms : 2600;
    var text = txt(message);
    try {
      var h = ensureHost();
      var it = mk("div", "wtoast-i", text);
      it.setAttribute("data-kind", kind);
      h.appendChild(it);
      items.push(it);
      while (items.length > TOAST_MAX) drop(items.shift());   // 超过 3 条：最早的淡出
      it._wtimer = setTimeout(function () { drop(it); }, ms);
    } catch (e) {
      try { console.log("[wdiag.toast] " + text); } catch (e2) {}
    }
  }

  window.wdiag = {
    confirm: confirmDialog,
    prompt: promptDialog,
    toast: toast
  };
})();
