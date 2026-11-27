const API_BASE = "http://127.0.0.1:8765";
let PROFILE_DATA = null;
let LAST_DM = "";

function setStatus(msg, isErr) {
  const el = document.getElementById("status-box");
  el.textContent = msg;
  el.className = "status " + (isErr ? "err" : "ok");
}

async function initPopup() {
  try {
    const res = await fetch(`${API_BASE}/api/profile`);
    const data = await res.json();
    PROFILE_DATA = data;
    const aiBadge = data.gemini_configured ? "✨ Gemini Ativo" : "📝 Modo Offline";
    document.getElementById("conn-state").textContent =
      `Ligado (${data.full_name || "Rui"}) · ${aiBadge}`;
  } catch (err) {
    document.getElementById("conn-state").textContent =
      "Dashboard local offline (inicia estagio_radar ui/watch)";
    setStatus("Não foi possível ligar a http://127.0.0.1:8765.", true);
    document.getElementById("btn-autofill").disabled = true;
    document.getElementById("btn-copy-note").disabled = true;
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function injectContentScript(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    files: ["content.js"],
  });
}

document.getElementById("btn-autofill").addEventListener("click", async () => {
  if (!PROFILE_DATA) return;
  const track = document.getElementById("cv-track").value;
  const btn = document.getElementById("btn-autofill");
  btn.disabled = true;

  try {
    const tab = await getActiveTab();
    await injectContentScript(tab.id);

    const ctx = await chrome.tabs.sendMessage(tab.id, { action: "GET_CONTEXT" });
    const genRes = await fetch(`${API_BASE}/api/generate-for-page`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: tab.url || "",
        title: (ctx && ctx.title) || tab.title || "",
        page_text: (ctx && ctx.pageText) || "",
        cv_track: track,
      }),
    });
    const kit = await genRes.json();

    const pdfRes = await fetch(`${API_BASE}/api/cv-file?track=${encodeURIComponent(track)}`);
    const pdfBuf = await pdfRes.arrayBuffer();
    const cvMeta = (PROFILE_DATA.cvs || []).find((c) => c.track === track);
    const cvFilename = (cvMeta && cvMeta.filename) || "CV_Rui_Sa.pdf";

    const fillRes = await chrome.tabs.sendMessage(tab.id, {
      action: "AUTOFILL",
      profile: PROFILE_DATA,
      coverNote: kit.short_cover_note || "",
      cvBytes: Array.from(new Uint8Array(pdfBuf)),
      cvFilename,
    });

    let extraAnsCount = 0;
    if (fillRes && fillRes.openQuestions && fillRes.openQuestions.length > 0) {
      const ansRes = await fetch(`${API_BASE}/api/answer-questions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company: kit.company || "",
          title: (ctx && ctx.title) || tab.title || "",
          page_text: (ctx && ctx.pageText) || "",
          questions: fillRes.openQuestions,
          cv_track: track,
        }),
      });
      const ansData = await ansRes.json();
      if (ansData && ansData.answers) {
        const applied = await chrome.tabs.sendMessage(tab.id, {
          action: "FILL_ANSWERS",
          answers: ansData.answers,
        });
        extraAnsCount = (applied && applied.count) || 0;
      }
    }

    const fieldsCount = ((fillRes && fillRes.filledFields) || []).length + extraAnsCount;
    const cvTxt = fillRes && fillRes.cvAttached ? ` + CV ${cvFilename} anexado` : "";
    setStatus(`Preenchidos ${fieldsCount} campo(s)${cvTxt}! Revê e carrega em Submeter.`, false);
  } catch (err) {
    setStatus("Erro ao preencher página: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-copy-note").addEventListener("click", async () => {
  const track = document.getElementById("cv-track").value;
  try {
    const tab = await getActiveTab();
    await injectContentScript(tab.id);
    const ctx = await chrome.tabs.sendMessage(tab.id, { action: "GET_CONTEXT" });
    const res = await fetch(`${API_BASE}/api/generate-for-page`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: tab.url || "",
        title: (ctx && ctx.title) || tab.title || "",
        page_text: (ctx && ctx.pageText) || "",
        cv_track: track,
      }),
    });
    const kit = await res.json();
    document.getElementById("gen-note").value = kit.short_cover_note || "";
    LAST_DM = kit.recruiter_dm || "";
    document.getElementById("note-card").style.display = "block";
    await navigator.clipboard.writeText(kit.short_cover_note || "");
    setStatus("Nota natural copiada para o clipboard!", false);
  } catch (err) {
    setStatus("Erro ao gerar nota: " + err.message, true);
  }
});

document.getElementById("btn-copy-dm").addEventListener("click", async () => {
  if (!LAST_DM) return;
  await navigator.clipboard.writeText(LAST_DM);
  setStatus("DM copiada! Cola no LinkedIn do recrutador.", false);
});

initPopup();
