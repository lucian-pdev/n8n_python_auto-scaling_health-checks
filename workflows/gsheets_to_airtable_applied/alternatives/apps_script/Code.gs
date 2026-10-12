// Apps Script read proxy for the gsheets_to_airtable workflow (fallback when there is no Google Cloud project).
// Setup: alternatives/README.md -> Apps Script. Bind it to the sheet (Extensions -> Apps Script), set TOKEN,
// Deploy -> Web app, Execute as: Me, Who has access: Anyone.
const TOKEN = 'PUT-RANDOM-HEX-HERE';

// GET ?token=..&tab=Signups[&header=2]   -> {columns, header_row, rows:[{row_number, <col>: value}]}
// GET ?token=..&file=<driveFileId>        -> {id, filename, contentType, size, base64}
// GET ?token=..&whoami=1                  -> account the web app runs as
function doGet(e) {
  const p = e.parameter;
  if (p.token !== TOKEN) return out({ error: 'forbidden' });
  try {
    if (p.whoami) return out({ effective_user: Session.getEffectiveUser().getEmail(),   // needs authorize() run once
                               sheet: SpreadsheetApp.getActive().getName() });
    return out(p.file ? file_(p.file) : rows_(p.tab || 'Sheet1', p.header));
  } catch (err) {
    return out({ error: String(err) });
  }
}

function file_(id) {
  const b = DriveApp.getFileById(id).getBlob();
  const bytes = b.getBytes();
  return { id: id, filename: b.getName(), contentType: b.getContentType(),
           size: bytes.length, base64: Utilities.base64Encode(bytes) };
}

function rows_(tab, headerParam) {
  const sh = SpreadsheetApp.getActive().getSheetByName(tab);
  if (!sh) return { error: 'no tab ' + tab };
  const range = sh.getDataRange();
  const shown = range.getDisplayValues(), rich = range.getRichTextValues();
  // header row: explicit ?header=N, else the first row with at least 2 non-empty cells
  let h = headerParam ? Number(headerParam) - 1 : shown.findIndex(r => r.filter(String).length >= 2);
  if (h < 0 || h >= shown.length) return { error: 'no header row' };
  // blank or repeated header cells must not overwrite each other: "Column C", "Name (2)"
  const seen = {};
  const columns = shown[h].map((name, j) => {
    let col = String(name).trim() || 'Column ' + colLetter_(j + 1);
    seen[col] = (seen[col] || 0) + 1;
    return seen[col] > 1 ? col + ' (' + seen[col] + ')' : col;
  });
  const rows = [];
  for (let i = h + 1; i < shown.length; i++) {
    if (!shown[i].some(String)) continue;                            // fully empty row
    const row = { row_number: i + 1 };
    columns.forEach((col, j) => { row[col] = rich[i][j].getLinkUrl() || shown[i][j]; });  // link URL beats display text
    rows.push(row);
  }
  return { columns: columns, header_row: h + 1, rows: rows };
}

function colLetter_(n) {
  let s = '';
  for (; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + (n - 1) % 26) + s;
  return s;
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

// Run once in the editor (Run -> authorize) so whoami may read the account email.
function authorize() { Logger.log(Session.getEffectiveUser().getEmail()); }
