"use strict";

const telegram = window.Telegram?.WebApp;
const headings = {
  schedule: "Ближайшие 7 дней", tasks: "Открытые задачи", reminders: "Мои напоминания",
};
const empty = {
  schedule: "На ближайшие 7 дней занятий нет.",
  tasks: "Открытых задач нет.",
  reminders: "Активных напоминаний нет.",
};
const heading = document.getElementById("heading");
const status = document.getElementById("status");
const source = document.getElementById("source");
const items = document.getElementById("items");
const refresh = document.getElementById("refresh");
const more = document.getElementById("more");
const tabs = document.querySelectorAll("[data-view]");
let view = "schedule";
let controller;
let nextOffset = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function dateLabel(value) {
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year}`;
}

function render(data) {
  let lastDay;
  for (const entry of data.items) {
    const card = element("article", "", "item");
    if (view === "schedule") {
      if (lastDay !== entry.date) {
        items.append(element("h3", dateLabel(entry.date), "day"));
        lastDay = entry.date;
      }
      card.append(
        element("p", entry.title),
        element("p", `${entry.start}–${entry.end} · ${entry.location}`, "meta"),
      );
    } else {
      const label = entry.when?.includes("T")
        ? `${dateLabel(entry.when.slice(0, 10))} ${entry.when.slice(11, 16)}`
        : entry.when || "Без срока";
      const repeat = {1: " · каждый день", 7: " · каждую неделю"}[entry.repeat] || "";
      card.append(
        element("p", entry.text),
        element("p", `${label} · ${entry.timezone}${repeat}`, "meta"),
      );
    }
    items.append(card);
  }
}

async function load(offset = 0) {
  if (!telegram?.initData) return;
  controller?.abort();
  const request = new AbortController();
  controller = request;
  heading.textContent = headings[view];
  refresh.disabled = true;
  more.disabled = true;
  if (!offset) {
    items.replaceChildren();
    source.hidden = true;
    more.hidden = true;
  }
  status.textContent = "Загружаем данные…";
  try {
    const response = await fetch("/miniapp/api", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({init_data: telegram.initData, view, offset}),
      signal: request.signal,
      cache: "no-store",
      credentials: "omit",
    });
    if (request.signal.aborted) return;
    if (response.status === 401 || response.status === 403) {
      status.textContent = "Откройте панель заново из чата с ботом.";
      return;
    }
    if (!response.ok) throw new Error("Request failed");
    const data = await response.json();
    if (request.signal.aborted) return;
    status.textContent = data.warning || (items.childElementCount || data.items.length ? "" : empty[view]);
    if (data.source) {
      source.textContent = `${data.source} · ${data.timezone}`;
      source.hidden = false;
    }
    render(data);
    nextOffset = data.next_offset;
    more.hidden = !data.more;
  } catch {
    if (!request.signal.aborted) status.textContent = "Не удалось загрузить данные. Попробуйте обновить.";
  } finally {
    if (!request.signal.aborted) {
      refresh.disabled = false;
      more.disabled = false;
    }
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
