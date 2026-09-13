/**
 * Small dependency-free CSV parser. Handles quoted fields (so a
 * `description`-style column containing commas doesn't break), assumes
 * well-formed UTF-8 text input.
 */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;

  const src = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (inQuotes) {
      if (c === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
      continue;
    }
    if (c === '"') inQuotes = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => !(r.length === 1 && r[0] === ""));
}

/**
 * Resolve a case-insensitive header lookup, mirroring init_db.py's
 * _read_csv: columnMap (canonical -> actual header text) overrides the
 * case-insensitive match for whichever canonical columns it covers.
 */
export function resolveHeader(
  header: string[],
  canonical: string,
  columnMap: Record<string, string>
): string | null {
  const mapped = columnMap[canonical];
  if (mapped !== undefined) {
    return header.includes(mapped) ? mapped : null;
  }
  const lower = header.find((h) => h.trim().toLowerCase() === canonical);
  return lower ?? null;
}

export function parseCsvFile(text: string): { header: string[]; rows: Record<string, string>[] } {
  const rawRows = parseCsv(text);
  if (rawRows.length === 0) return { header: [], rows: [] };
  const header = rawRows[0].map((h) => h.trim());
  const rows = rawRows.slice(1).map((r) => {
    const rec: Record<string, string> = {};
    header.forEach((h, idx) => {
      rec[h] = r[idx] ?? "";
    });
    return rec;
  });
  return { header, rows };
}
