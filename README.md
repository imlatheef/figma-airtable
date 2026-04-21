# Airtable → Figma → Airtable  |  Design Automation

Automatically generate a JPG design from your Figma template whenever a new
Airtable record is created, then upload the JPG back to the record.

```
New Airtable record
      │
      ▼
Airtable Automation fires webhook
      │
      ▼
Python server receives record_id
      │
      ├─► Fetch fields from Airtable API
      ├─► Export template frame from Figma API
      ├─► Overlay field values onto the image
      │
      ▼
Upload JPG → Airtable attachment field  ✓
```

---

## Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | https://python.org |
| ngrok (free) | https://ngrok.com/download |

---

## 1  Install dependencies

```bash
cd airtable_figma_automation
pip install -r requirements.txt
```

---

## 2  Get your API keys

### Airtable
1. Go to **airtable.com/create/tokens**
2. Create a token with scopes: `data.records:read`, `data.records:write`
3. Add your base to the token's access list
4. Copy the token → paste into `config.yaml` → `airtable.api_key`

**Base ID**: open your base in Airtable, look at the URL:
`https://airtable.com/appXXXXXXXXXXXXXX/...`  → `appXXXXXXXXXXXXXX`

**Attachment field**: create (or use) an **Attachment** type field in your table
(e.g. "Generated Design"). This is where the JPG will be uploaded.

### Figma
1. Go to **figma.com → Account Settings → Personal Access Tokens**
2. Create a token → paste into `config.yaml` → `figma.api_key`

**File key**: open your Figma file, look at the URL:
`https://www.figma.com/file/AbCdEfGhIjKlMnOp/...`  → `AbCdEfGhIjKlMnOp`

**Frame node ID**:
1. Click on the template frame in Figma
2. Right-click → "Copy link to selection"
3. The link ends with `?node-id=12-345` → use `12:345` (replace `-` with `:`)

---

## 3  Prepare your Figma template

Name each text layer in Figma **exactly** as it appears in `field_mappings`
(right-hand side).

Example Figma Layers panel:
```
▼ Template Frame
    name_text       ← text layer named "name_text"
    title_text
    company_text
    [logo]          ← static image, not touched
```

**Tip**: Keep the text layers **empty** in Figma, or with short placeholder text.
The script will sample the background color behind each text node to erase
the placeholder before writing the real value.

---

## 4  Fill in config.yaml

```yaml
airtable:
  api_key: "patXXX..."
  base_id: "appXXX..."
  table_name: "Contacts"
  attachment_field: "Generated Design"

figma:
  api_key: "figd_XXX..."
  file_key: "AbCdEfGhIjKlMnOp"
  frame_node_id: "12:345"
  export_scale: 2          # 1=72dpi  2=144dpi  3=216dpi

server:
  port: 5000
  webhook_secret: "my-secret-123"   # optional but recommended

field_mappings:
  "Full Name": "name_text"          # Airtable field : Figma layer name
  "Job Title": "title_text"
  "Company":   "company_text"
```

---

## 5  Start the server

```bash
python main.py
```

You'll see:
```
INFO     Starting webhook server on port 5000
INFO     Expose this server to the internet with ngrok:
           ngrok http 5000
```

In a **second terminal**, run ngrok:

```bash
ngrok http 5000
```

ngrok will show a public URL like `https://abc123.ngrok-free.app`.

---

## 6  Set up Airtable Automation

1. Open your Airtable base → click **Automations** (top-right)
2. **+ New automation**

| Setting | Value |
|---------|-------|
| Trigger | **When a record is created** |
| Table | your table |
| Action | **Send a webhook** |
| URL | `https://abc123.ngrok-free.app/webhook` |
| Method | POST |
| Body (JSON) | `{ "record_id": "{{Record ID}}" }` |
| Header (optional) | `X-Webhook-Secret: my-secret-123` |

3. Click **Test** → check your terminal for `✓ Design uploaded`
4. **Turn on** the automation

---

## 7  Test it

Create a new record in Airtable. Within a few seconds you should see:

```
INFO  pipeline    Record recXXX fields: ['Name', 'Title', 'Company']
INFO  pipeline    Rendered JPEG  size=284321 bytes
INFO  pipeline    ✓ Design uploaded to Airtable record recXXX
```

And the "Generated Design" field in that record will contain the JPG.

---

## Other run modes

```bash
# Process a specific record immediately (no server needed)
python main.py --record recXXXXXXXXXXXXXX

# Backfill: process all records that don't yet have a design
python main.py --backfill

# Use a different config file
python main.py --config production.yaml
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Figma layer 'name_text' not found` | Check the layer name in Figma Layers panel – it's case-sensitive |
| `No image URL returned` | Check your `frame_node_id` – use `:` not `-` as separator |
| Text not replacing correctly | Make text layers empty (no placeholder) in Figma for cleanest results |
| Attachment upload 403 | See note below about Airtable plans |
| ngrok URL keeps changing | Use a paid ngrok account for a stable URL, or self-host with a fixed IP |

### Airtable attachment upload & plans

- **Free / Plus / Pro**: the script uses a direct upload (Content API). If your plan
  doesn't support it, it falls back to a base64 PATCH (works for images up to ~1 MB).
- **Enterprise**: full Content API is available, no size limit.
- If you hit size limits: reduce `export_scale` to `1` in config (halves file size).

---

## File overview

```
airtable_figma_automation/
├── main.py              Entry point (CLI)
├── webhook_server.py    Flask server that receives Airtable webhooks
├── pipeline.py          Orchestrates fetch → render → upload per record
├── figma_client.py      Figma REST API: export frame, read text nodes
├── airtable_client.py   Airtable REST API: fetch records, upload attachments
├── image_renderer.py    Pillow-based text overlay engine
├── config.yaml          Your configuration (API keys, field mappings)
└── requirements.txt     Python dependencies
```
