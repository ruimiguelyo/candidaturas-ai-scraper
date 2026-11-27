(() => {
  if (window.__estagioRadarExtLoaded) return;
  window.__estagioRadarExtLoaded = true;

  function getDescriptor(el) {
    const id = el.id || "";
    const ariaLblBy = el.getAttribute("aria-labelledby") || "";
    const refEl = ariaLblBy ? document.getElementById(ariaLblBy.split(/\s+/)[0]) : null;
    const labelEl = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
    const parentLbl = el.closest("label");
    const cont = el.closest("div, li, fieldset, section, td");
    const contLbl = cont ? cont.querySelector("label, legend, span[class*='label' i]") : null;
    const lblText = (labelEl?.innerText || refEl?.innerText || parentLbl?.innerText || contLbl?.innerText || "").trim();
    return {
      label: lblText,
      full: [el.name || "", id, el.placeholder || "", el.getAttribute("aria-label") || "", el.autocomplete || "", lblText]
        .join(" | ")
        .toLowerCase(),
    };
  }

  function setNativeValue(el, val) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, val);
    else el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function matchInputKind(descLower, tag, type) {
    if (type === "file") {
      if (/photo|avatar|picture|image|cover/.test(descLower)) return null;
      return "resume";
    }
    if (tag === "textarea") {
      if (/cover|carta|why|motivation|motivacao|additional|message|summary/.test(descLower)) return "cover_note";
      return "open_question";
    }
    if (["hidden", "checkbox", "radio", "submit", "button"].includes(type)) return null;
    if ((descLower.includes("first") && descLower.includes("name")) || descLower.includes("primeiro nome") || descLower.includes("given-name")) return "first_name";
    if ((descLower.includes("last") && descLower.includes("name")) || descLower.includes("apelido") || descLower.includes("surname") || descLower.includes("family-name")) return "last_name";
    if (descLower.includes("full name") || descLower.includes("nome completo") || /(?:^|\b|\|)(?:name|nome)(?:$|\b|\|)/.test(descLower)) {
      if (!/company|empresa|university|school|degree|file/.test(descLower)) return "full_name";
    }
    if (type === "email" || descLower.includes("email") || descLower.includes("e-mail")) return "email";
    if (type === "tel" || /phone|mobile|telefone|telemovel|telemóvel/.test(descLower)) return "phone";
    if (descLower.includes("linkedin")) return "linkedin_url";
    if (descLower.includes("github")) return "github_url";
    if (/portfolio|website|personal site|blog/.test(descLower)) return "portfolio_url";
    if (/city|cidade|location|localizacao|localização|morada/.test(descLower)) return "location";
    if (/university|school|college|faculdade|universidade/.test(descLower)) return "university";
    if (/degree|curso|licenciatura|major/.test(descLower)) return "degree";
    return null;
  }

  const QUESTION_ELEMENTS = new Map();

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.action === "GET_CONTEXT") {
      sendResponse({
        title: document.title || "",
        pageText: (document.body ? document.body.innerText : "").slice(0, 4000),
      });
      return true;
    }

    if (msg.action === "AUTOFILL") {
      const p = msg.profile || {};
      const values = {
        first_name: p.first_name || "",
        last_name: p.last_name || "",
        full_name: p.full_name || "",
        email: p.email || "",
        phone: p.phone || "",
        linkedin_url: p.linkedin_url || "",
        github_url: p.github_url || "",
        portfolio_url: p.portfolio_url || "",
        location: p.location || "",
        university: p.university || "",
        degree: p.degree || "",
        cover_note: msg.coverNote || "",
      };

      const filledFields = [];
      const openQuestions = [];
      let cvAttached = false;
      QUESTION_ELEMENTS.clear();

      document.querySelectorAll("input, textarea").forEach((el) => {
        if (el.disabled || el.readOnly) return;
        const type = (el.getAttribute("type") || "text").toLowerCase();
        const rect = el.getBoundingClientRect();
        if (type !== "file" && (rect.width === 0 || rect.height === 0)) return;

        const desc = getDescriptor(el);
        const kind = matchInputKind(desc.full, el.tagName.toLowerCase(), type);
        if (kind === "resume" && !cvAttached && msg.cvBytes && msg.cvBytes.length) {
          try {
            const bytes = new Uint8Array(msg.cvBytes);
            const file = new File([bytes], msg.cvFilename || "CV.pdf", { type: "application/pdf" });
            const dt = new DataTransfer();
            dt.items.add(file);
            el.files = dt.files;
            el.dispatchEvent(new Event("change", { bubbles: true }));
            cvAttached = true;
            filledFields.push("cv_pdf");
          } catch (_e) {}
          return;
        }
        if (kind === "open_question" && !el.value.trim() && desc.label) {
          const q = desc.label.slice(0, 220);
          openQuestions.push(q);
          QUESTION_ELEMENTS.set(q, el);
          return;
        }
        if (kind && values[kind] && !el.value.trim()) {
          setNativeValue(el, values[kind]);
          filledFields.push(kind);
        }
      });

      document.querySelectorAll("select").forEach((sel) => {
        if (sel.disabled) return;
        const desc = getDescriptor(sel).full;
        let tokens = null;
        let label = "";
        if (/country|pais|país/.test(desc)) { tokens = ["portugal"]; label = "country"; }
        else if (/sponsor|visa|visto/.test(desc)) { tokens = ["no", "não", "nao"]; label = "sponsorship"; }
        else if (/authori|eligible|work in|trabalhar/.test(desc)) { tokens = ["yes", "sim"]; label = "work_auth"; }
        else if (/graduation|grad year/.test(desc)) { tokens = ["2027"]; label = "graduation_year"; }
        if (!tokens) return;
        const opt = Array.from(sel.options).find((o) => o.value && tokens.some((t) => o.text.toLowerCase().includes(t)));
        if (opt) {
          sel.value = opt.value;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          filledFields.push(label);
        }
      });

      sendResponse({ filledFields, cvAttached, openQuestions });
      return true;
    }

    if (msg.action === "FILL_ANSWERS") {
      let count = 0;
      const answers = msg.answers || {};
      Object.entries(answers).forEach(([q, ans]) => {
        const el = QUESTION_ELEMENTS.get(q);
        if (el && ans && !el.value.trim()) {
          setNativeValue(el, ans);
          count += 1;
        }
      });
      sendResponse({ count });
      return true;
    }
  });
})();
