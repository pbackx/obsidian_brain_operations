# Brain Operations - Dossier Dashboard

> **Overview:**  
> This dashboard dynamically surfaces living dossiers generated and maintained by Antigravity.  
> It updates automatically every time you open this note — **zero manual maintenance required**.

---

## 🕒 Dossiers Updated Recently (Last 48 Hours)

```dataview
TABLE
    updated AS "Updated Date",
    status AS "Status",
    source_note AS "Source Note"
FROM "Brain_Operations/Dossiers"
WHERE updated >= date(today) - dur(2 days) AND file.folder = "Brain_Operations/Dossiers" AND !contains(file.path, "Archive")
SORT updated DESC
```

---

## 📋 All Living Dossiers (Sorted by Recency)

```dataview
TABLE
    updated AS "Last Active",
    status AS "Status",
    task AS "Original Task"
FROM "Brain_Operations/Dossiers"
WHERE file.folder = "Brain_Operations/Dossiers" AND !contains(file.path, "Archive")
SORT updated DESC
```

---

## ❓ Dossiers Awaiting User Clarification (`#ai/question`)

```dataview
TABLE
    updated AS "Last Active",
    source_note AS "Source Note",
    task AS "Original Task"
FROM "Brain_Operations/Dossiers"
WHERE status = "needs-clarification" AND file.folder = "Brain_Operations/Dossiers" AND !contains(file.path, "Archive")
SORT updated DESC
```

---

## 📦 Archived Dossiers

```dataview
TABLE
    updated AS "Last Active",
    status AS "Status",
    task AS "Original Task"
FROM "Brain_Operations/Dossiers"
WHERE contains(file.folder, "Archive")
SORT updated DESC
```
