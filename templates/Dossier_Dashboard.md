# Brain Operations - Task & Dossier Dashboard

> **Overview:**  
> Every task is a 1-file-per-task Markdown note (`type: task`) across `TODO/`, `<Domain>/TODO/`, `Freewrite/`, and `TODO/Archive/`.  
> Only tasks placed in **`status: ai-queue`** are picked up by the nightly agent.

---

## 🗂️ Master Obsidian Base & Kanban Board (`TODO/Tasks.base`)

Open [[TODO/Tasks.base|Tasks.base]] directly for the full-width **Kanban Board**, or use the embedded Base view below (switch between *📋 All Active Kanban*, *🤖 AI Workflow Board*, *❓ Needs My Input*, domain tabs, and *📦 Archive* in the top-left view selector):

![[TODO/Tasks.base]]

---

## 🕒 AI Tasks Updated Recently (Last 48 Hours)

```dataview
TABLE
    updated AS "Updated Date",
    status AS "Status",
    category AS "Category",
    summary AS "Latest Summary"
FROM !"Templates"
WHERE type = "task" AND updated >= date(today) - dur(2 days) AND status != "archived"
SORT updated DESC
```

---

## ❓ Tasks Awaiting Your Input (`status: needs-clarification`)

```dataview
TABLE
    updated AS "Last Active",
    category AS "Category",
    question AS "Pending Question",
    summary AS "Latest Summary"
FROM !"Templates"
WHERE type = "task" AND status = "needs-clarification"
SORT updated DESC
```

---

## 🤖 Queued for Tonight's Nightly Run (`status: ai-queue`)

```dataview
TABLE
    priority AS "Priority",
    category AS "Category",
    created AS "Created"
FROM !"Templates"
WHERE type = "task" AND status = "ai-queue"
SORT priority DESC, updated DESC
```
