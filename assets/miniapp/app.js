"use strict";

const telegram = window.Telegram?.WebApp;
const byId = id => document.getElementById(id);
const headings = {
  schedule: "Расписание", tasks: "Открытые задачи", reminders: "Мои напоминания",
  weather: "Погода сейчас",
};
const empty = {
  schedule: "На эти даты занятий нет.", tasks: "Открытых задач нет.",
  reminders: "Активных напоминаний нет.", weather: "Погода временно недоступна.",
};
const heading = byId("heading");
const status = byId("status");
const source = byId("source");
const items = byId("items");
const completed = byId("completed");
const refresh = byId("refresh");
const more = byId("more");
const toolbar = byId("toolbar");
const tabs = document.querySelectorAll("[data-view]");
let view = "schedule";
let personal = false;
let week = 0;
let nextOffset = 0;
let controller;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(label, action, className, description) {
  const node = element("button", label, className);
  node.type = "button";
  if (description) node.setAttribute("aria-label", description);
  node.addEventListener("click", action);
  return node;
}

function dateLabel(value) {
  return value.split("-").reverse().join(".");
}

function datePlus(value, days) {
  const date = new Date(value + "T12:00:00Z");
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

async function api(payload, signal) {
  const response = await fetch("/miniapp/api", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({init_data: telegram.initData, ...payload}),
    signal, cache: "no-store", credentials: "omit",
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("Откройте панель заново из чата с ботом.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message || data.error || "Не удалось выполнить запрос.");
  return data;
}

function modal(name, submit) {
  const dialog = byId(name);
  const form = byId(name + "-form");
  const send = byId(name + "-submit");
  const cancel = byId(name + "-close");
  const feedback = byId(name === "editor" ? "form-status" : "review-status");
  feedback.textContent = "";
  cancel.onclick = () => dialog.close();
  dialog.oncancel = event => { if (send.disabled) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    send.disabled = cancel.disabled = true;
    feedback.textContent = "Выполняем…";
    try {
      await submit(Object.fromEntries(new FormData(form)));
      dialog.close();
    } catch (error) {
      feedback.textContent = error.message || "Попробуйте ещё раз.";
    } finally {
      send.disabled = cancel.disabled = false;
    }
  };
  dialog.showModal();
}

function field(name, title, type, value = "", options = {}) {
  const label = element("label", title);
  const input = document.createElement(Array.isArray(options) ? "select" : "input");
  input.name = name;
  if (Array.isArray(options)) {
    for (const [key, text] of options) {
      const option = element("option", text);
      option.value = key;
      input.append(option);
    }
  } else {
    input.type = type;
    Object.assign(input, options);
  }
  input.value = value;
  label.append(input);
  return label;
}

function editForm(title, fields, submit, submitText = "Продолжить") {
  byId("editor-title").textContent = title;
  const inputs = byId("fields");
  inputs.replaceChildren(...fields);
  byId("editor-submit").textContent = submitText;
  byId("editor").onclose = () => inputs.replaceChildren();
  modal("editor", submit);
}

function review(message, confirm) {
  byId("review-text").textContent = message;
  modal("review", async () => {
    const result = await confirm();
    telegram.HapticFeedback?.notificationOccurred("success");
    load(0, result.message);
  });
}

async function prepare(change) {
  const data = await api({action: "prepare", request_id: crypto.randomUUID(), change});
  review(data.message, () => api({action: "confirm", domain: data.domain, pending: data.pending}));
}

async function quick(change, target) {
  target.disabled = true;
  try {
    await prepare(change);
  } catch (error) {
    status.textContent = error.message;
  } finally {
    target.disabled = false;
  }
}

function lessonForm(item, day) {
  editForm(item ? "Редактировать занятие" : "Добавить занятие", [
    field("title", "Предмет", "text", item?.title, {required: true, maxLength: 200}),
    field("date", "Дата", "date", item?.date || day, {required: true}),
    field("start", "Начало", "time", item?.start, {required: true}),
    field("end", "Конец", "time", item?.end, {required: true}),
    field("location", "Аудитория или место", "text", item?.location,
      {required: true, maxLength: 200}),
  ], lesson => prepare({kind: "lesson", operation: "save", id: item?.id, version: item?.version,
    lesson}));
}

function recordForm(kind, item, moving = false) {
  const task = kind === "t";
  const daily = kind === "d";
  const type = daily ? "time" : "datetime-local";
  const timing = daily ? item?.when : item?.when?.slice(0, 16);
  const when = field("when", daily ? "Время каждый день" : task ? "Дедлайн" : "Дата и время",
    type, timing, {required: !task || moving});
  const fields = moving ? [when] : [
    field("text", "Название", "text", item?.text, {required: true, maxLength: 500}), when,
  ];
  if (task && !moving) {
    fields.push(
      field("repeat", "Повтор", "", String(item?.repeat || ""),
        [["", "Разовая задача"], ["1", "Каждый день"], ["7", "Каждую неделю"]]),
      field("lead", "Оповестить за N минут до дедлайна", "number", item?.lead || "",
        {min: 1, max: 10080}),
    );
  }
  const title = moving ? "Перенести запись" : item ? "Редактировать запись"
    : task ? "Новая задача" : daily ? "Ежедневное напоминание" : "Разовое напоминание";
  fields.push(element("p", item?.timezone ? "Часовой пояс · " + item.timezone
    : "Время по часовому поясу бота (/timezone).", "meta"));
  editForm(title, fields, values => prepare({
    kind, operation: moving ? "move" : "save", id: item?.id, version: item?.version,
    text: values.text, when: values.when, repeat: Number(values.repeat) || undefined,
    lead: Number(values.lead) || undefined,
  }));
}

function connectItmo() {
  let ticket;
  editForm("Подключить ИТМО", [
    element("p", "Войдите в ITMO.ID. После входа панель попросит подтвердить привязку.", "meta"),
    field("username", "Логин ITMO.ID", "text", "",
      {required: true, maxLength: 128, autocomplete: "username"}),
    field("password", "Пароль", "password", "",
      {required: true, maxLength: 1024, autocomplete: "current-password"}),
  ], async credentials => {
    try {
      ticket ||= await api({action: "itmo_issue"});
      const result = await api({action: "itmo_sign_in", ticket: ticket.ticket, ...credentials});
      const id = ticket.id;
      ticket = null;
      review("Привязать аккаунт ИТМО " + result.account + "?", async () => {
        const confirmed = await api({action: "itmo_confirm", id});
        personal = false;
        week = 0;
        return confirmed;
      });
    } finally {
      byId("editor-form").elements.password.value = "";
    }
  }, "Войти");
}

function renderToolbar(data) {
  toolbar.replaceChildren();
  if (view === "schedule") {
    const isItmo = data.connected && !personal;
    if (data.connected) {
      for (const [label, value] of [["ИТМО", false], ["Личное", true]]) {
        const choice = button(label, () => { personal = value; load(); });
        choice.setAttribute("aria-pressed", String(personal === value));
        toolbar.append(choice);
      }
    }
    const previous = button("‹", () => { week -= 7; load(); }, "", "Предыдущие 7 дней");
    const next = button("›", () => { week += 7; load(); }, "", "Следующие 7 дней");
    previous.disabled = week === 0;
    next.disabled = week === 364;
    toolbar.append(previous, next);
    if (!isItmo) toolbar.append(button("+ Занятие", () => lessonForm(null, data.first), "primary"));
    toolbar.append(button(data.connected ? "Переподключить ИТМО" : "Подключить ИТМО", connectItmo));
  } else if (view === "tasks") {
    toolbar.append(button("+ Задача", () => recordForm("t"), "primary"));
  } else if (view === "reminders") {
    toolbar.append(
      button("+ Разовое", () => recordForm("r"), "primary"),
      button("+ Ежедневное", () => recordForm("d")),
    );
  }
  toolbar.hidden = !toolbar.childElementCount;
}

function renderLessons(data) {
  let lastDay;
  for (const entry of data.items) {
    if (lastDay !== entry.date) {
      items.append(element("h3", dateLabel(entry.date), "day"));
      lastDay = entry.date;
    }
    const card = element("article", "", "item");
    card.append(
      element("p", entry.title, "item-title"),
      element("p", entry.start + "–" + entry.end + " · " + entry.location, "meta"),
    );
    if (entry.id) {
      const actions = element("div", "", "toolbar");
      actions.append(button("Изменить", () => lessonForm(entry)));
      actions.append(button("Удалить", event => quick({
        kind: "lesson", operation: "delete", id: entry.id, version: entry.version,
      }, event.currentTarget)));
      card.append(actions);
    }
    items.append(card);
  }
}

function renderRecord(entry, done = false) {
  const card = element("article", "", "item item-line" + (done ? " done" : ""));
  const content = element("div", "");
  if (entry.kind === "t") {
    card.append(button(done ? "✓" : "○", event => quick({
      kind: "t", operation: done ? "restore" : "done", id: entry.id, version: entry.version,
    }, event.currentTarget), "check", done ? "Вернуть в открытые" : "Отметить готовой"));
  }
  const when = entry.when?.includes("T")
    ? dateLabel(entry.when.slice(0, 10)) + " " + entry.when.slice(11, 16)
    : entry.when || "Без срока";
  const repeat = {1: " · каждый день", 7: " · каждую неделю"}[entry.repeat] || "";
  content.append(element("p", entry.text, "item-title"),
    element("p", when + " · " + entry.timezone + repeat, "meta"));
  if (entry.lead) content.append(element("p", "Оповещение за " + entry.lead + " мин.", "meta"));
  if (!done) {
    const actions = element("div", "", "toolbar");
    actions.append(button("Изменить", () => recordForm(entry.kind, entry)));
    actions.append(button("Перенести", () => recordForm(entry.kind, entry, true)));
    content.append(actions);
  }
  card.append(content);
  return card;
}

function weatherConditions(code) {
  if (code >= 95) return ["Гроза", "По возможности переждите грозу в помещении."];
  if ([56, 57, 66, 67].includes(code)) return ["Ледяной дождь", "На улице может быть скользко."];
  if ([71, 73, 75, 77, 85, 86].includes(code)) return ["Снег", "Выберите непромокаемую обувь."];
  if (code >= 51) return ["Дождь или морось", "Возьмите зонт или дождевик."];
  if (code === 45 || code === 48) return ["Туман", "Учтите ограниченную видимость."];
  return [["Ясно", "Преимущественно ясно", "Переменная облачность", "Пасмурно"][code]
    || "Условия не определены"];
}

function renderWeather(data) {
  const current = data.weather;
  const [description, advice] = weatherConditions(current.weather_code);
  const temp = current.temperature_c;
  const tips = [temp < 10 ? "Пригодится тёплая верхняя одежда."
    : temp >= 25 ? "Возьмите воду, если надолго выходите на улицу."
    : "Для улицы подойдёт лёгкий дополнительный слой одежды."];
  if (advice) tips.push(advice);
  if (current.wind_speed >= 10) tips.push("Ветрено. Пригодится непродуваемая куртка.");
  const list = element("ul", "", "weather-tips");
  list.append(...tips.map(tip => element("li", tip)));
  const card = element("article", "", "item");
  card.append(
    element("p", current.place.name),
    element("p", (temp > 0 ? "+" : "") + temp + " °C", "weather-temp"),
    element("p", description + " · ветер " + current.wind_speed + " " + current.wind_unit, "meta"),
    list,
    element("p", current.source + " · " + (current.observed_at || "").replace("T", " "), "meta"),
  );
  if (data.default_city) card.append(element("p", "Город по умолчанию. Изменить в чате: /city.", "meta"));
  items.append(card);
}

async function load(offset = 0, notice = "") {
  if (!telegram?.initData) return;
  controller?.abort();
  const request = new AbortController();
  controller = request;
  const firstPage = !offset || view === "schedule";
  heading.textContent = headings[view];
  refresh.disabled = more.disabled = true;
  if (firstPage) {
    items.replaceChildren();
    completed.replaceChildren();
    toolbar.hidden = source.hidden = more.hidden = true;
  }
  status.classList.remove("status-ok");
  status.textContent = "Загружаем данные…";
  try {
    const data = await api({
      view: view === "schedule" && personal ? "personal_schedule" : view,
      offset: view === "schedule" ? week : offset,
    }, request.signal);
    if (request.signal.aborted) return;
    if (firstPage) renderToolbar(data);
    if (data.source) {
      source.textContent = data.source + " · " + data.timezone;
      source.hidden = false;
      heading.textContent = dateLabel(data.first) + " — " + dateLabel(datePlus(data.first, 6));
    }
    if (view === "schedule") renderLessons(data);
    else if (data.weather) renderWeather(data);
    else items.append(...data.items.map(entry => renderRecord(entry)));
    if (data.completed?.length) {
      completed.append(element("h3", "Недавно выполнено", "day"));
      completed.append(...data.completed.map(entry => renderRecord(entry, true)));
    }
    status.textContent = notice || data.warning || (items.childElementCount ? "" : empty[view]);
    if (notice) status.classList.add("status-ok");
    nextOffset = data.next_offset;
    more.hidden = !data.more;
  } catch (error) {
    if (!request.signal.aborted) status.textContent = error.message || "Не удалось загрузить данные.";
  } finally {
    if (!request.signal.aborted) refresh.disabled = more.disabled = false;
  }
}

telegram?.ready();
if (telegram?.initData) {
  telegram.expand();
  refresh.hidden = false;
  refresh.addEventListener("click", () => load());
  more.addEventListener("click", () => load(nextOffset));
  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      view = tab.dataset.view;
      for (const item of tabs) item.setAttribute("aria-pressed", String(item === tab));
      load();
    });
  }
  load();
} else {
  for (const tab of tabs) tab.disabled = true;
}
