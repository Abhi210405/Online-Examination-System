document.addEventListener("DOMContentLoaded", () => {
  const pad = (part) => String(part).padStart(2, "0");
  document.querySelectorAll("[data-local-datetime]").forEach((input) => {
    const stored = input.dataset.utcValue;
    if (stored) {
      const date = new Date(`${stored}${stored.endsWith("Z") ? "" : "Z"}`);
      input.value = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }
    input.form?.addEventListener("submit", () => {
      const hidden = input.form.querySelector(`input[type="hidden"][name="${input.name.replace("_local", "")}"]`);
      hidden.value = input.value ? new Date(input.value).toISOString() : "";
      input.removeAttribute("name");
    }, { once: true });
  });

  document.querySelectorAll("[data-local-time]").forEach((element) => {
    const raw = element.dataset.localTime;
    const date = new Date(`${raw}${raw.endsWith("Z") ? "" : "Z"}`);
    if (Number.isNaN(date.getTime())) return;
    const options = element.hasAttribute("data-date-only")
      ? { dateStyle: "medium" }
      : { dateStyle: "medium", timeStyle: "short" };
    element.textContent = new Intl.DateTimeFormat(undefined, options).format(date);
  });

  const navToggle = document.querySelector("[data-nav-toggle]");
  const topnav = document.querySelector("[data-topnav]");

  navToggle?.addEventListener("click", () => {
    const expanded = navToggle.getAttribute("aria-expanded") === "true";
    navToggle.setAttribute("aria-expanded", String(!expanded));
    topnav?.classList.toggle("is-open", !expanded);
  });

  document.querySelectorAll("[data-confirm]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (!window.confirm(element.dataset.confirm)) event.preventDefault();
    });
  });

  const form = document.querySelector("[data-exam-form]");
  if (!form) return;

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
  const timer = document.querySelector("[data-exam-timer]");
  const timerValue = document.querySelector("[data-timer-value]");
  const saveStatus = document.querySelector("[data-save-status]");
  const deadline = Number(document.querySelector('meta[name="exam-deadline"]')?.content) * 1000;
  const queueByQuestion = new Map();
  let submitting = false;

  function updateProgress() {
    const questions = [...form.querySelectorAll("[data-question-id]")];
    const answered = new Set(
      questions.filter((input) => input.checked).map((input) => input.dataset.questionId),
    );
    document.querySelector("[data-answered-count]").textContent = String(answered.size);
    document.querySelector("[data-progress-bar]").style.width =
      `${questions.length ? (answered.size / new Set(questions.map((input) => input.dataset.questionId)).size) * 100 : 0}%`;
    document.querySelectorAll("[data-question-nav]").forEach((link) => {
      link.classList.toggle("is-answered", answered.has(link.dataset.questionNav));
    });
    form.querySelectorAll(".exam-option").forEach((label) => {
      label.classList.toggle("is-selected", label.querySelector("input").checked);
    });
  }

  async function saveAnswer(input) {
    const questionId = input.dataset.questionId;
    const optionId = input.checked ? Number(input.value) : null;
    const previous = queueByQuestion.get(questionId) || Promise.resolve();
    const queued = previous.catch(() => undefined).then(async () => {
      if (submitting) return;
      saveStatus.textContent = "Saving…";
      timer.classList.remove("save-error");
      const response = await fetch(form.dataset.autosaveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ question_id: Number(questionId), option_id: optionId }),
      });
      const payload = await response.json();
      if (payload.redirect) {
        window.location.assign(payload.redirect);
        return;
      }
      if (!response.ok) throw new Error(payload.error || "Your answer could not be saved.");
      saveStatus.textContent = "All changes saved";
    });
    queueByQuestion.set(questionId, queued);
    queued.catch((error) => {
      saveStatus.textContent = error.message || "Connection issue — try selecting again.";
      timer.classList.add("save-error");
    });
    return queued;
  }

  form.querySelectorAll("[data-question-id]").forEach((input) => {
    input.addEventListener("change", () => {
      updateProgress();
      saveAnswer(input).catch(() => undefined);
    });
  });

  form.addEventListener("submit", (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }
    submitting = true;
    form.querySelectorAll("button[type=submit]").forEach((button) => {
      button.disabled = true;
      button.textContent = "Submitting…";
    });
  });

  updateProgress();

  let autoSubmitStarted = false;
  async function tick() {
    const seconds = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    timerValue.textContent = `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
    if (seconds <= 60) timer.classList.add("timer-warning");
    if (seconds > 0 || autoSubmitStarted) return;
    autoSubmitStarted = true;
    saveStatus.textContent = "Time is up — submitting your saved answers…";
    await Promise.allSettled([...queueByQuestion.values()]);
    form.requestSubmit();
  }

  tick();
  window.setInterval(tick, 1000);
});
