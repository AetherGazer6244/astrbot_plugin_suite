const bridge = window.AstrBotPluginPage;

const el = {
  qr: document.getElementById("qrcode"),
  status: document.getElementById("status"),
  refresh: document.getElementById("refresh"),
  poll: document.getElementById("poll"),
};

let qrKey = "";
let timer = null;

function setStatus(text, tone) {
  el.status.textContent = text;
  el.status.className = "status" + (tone ? " " + tone : "");
}

function stopPolling() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function startPolling() {
  stopPolling();
  timer = setInterval(() => {
    pollOnce();
  }, 2000);
}

async function generate() {
  stopPolling();
  try {
    setStatus("正在向 B站 申请二维码…");
    const data = await bridge.apiGet("login/generate");
    if (!data || !data.url || !data.key) throw new Error("接口没有返回二维码");
    qrKey = data.key;
    el.qr.innerHTML = "";
    // QRCode 来自 CDN 上的 qrcodejs（index.html 里带 SRI）
    new QRCode(el.qr, {
      text: data.url,
      width: 220,
      height: 220,
      colorDark: "#0b1220",
      colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M,
    });
    setStatus("等待扫码", "pending");
    startPolling();
  } catch (e) {
    setStatus("生成失败：" + describe(e), "error");
  }
}

function describe(e) {
  return e && e.message ? e.message : String(e);
}

async function pollOnce() {
  if (!qrKey) return false;
  try {
    const data = await bridge.apiGet("login/poll", { key: qrKey });
    if (!data) throw new Error("接口没有返回状态");
    setStatus(data.text || "状态未知", data.login ? "ok" : "pending");
    if (data.login) {
      stopPolling();
      setStatus("✅ 已登录，登录态已写入插件配置", "ok");
      return true;
    }
    if (data.code === 86038) {
      await generate(); // generate 内部会重新开轮询
    }
  } catch (e) {
    setStatus("查询失败：" + describe(e), "error");
  }
  return false;
}

el.refresh.addEventListener("click", generate);
el.poll.addEventListener("click", pollOnce);

(async () => {
  if (!bridge) {
    setStatus("页面 Bridge 未就绪，请刷新 WebUI 页面", "error");
    return;
  }
  await bridge.ready();
  await generate();
})();
