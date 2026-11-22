/**
 * Per-run report: one row per feature, screenshots on failure, JSON + console.
 */
import fs from 'node:fs';
import path from 'node:path';

export class Report {
  constructor(dir) {
    this.dir = dir;
    this.rows = [];
    fs.mkdirSync(dir, { recursive: true });
  }

  record(feature, result) {
    const row = {
      id: feature.id,
      name: feature.name,
      status: result.status,
      duration_ms: result.durationMs ?? 0,
      error: result.error ? String(result.error.stack || result.error) : null,
      screenshot: result.screenshot ?? null,
      reason: result.reason ?? null
    };
    this.rows.push(row);
    const suffix = row.reason ? ` — ${row.reason}` : '';
    console.log(`[${row.status}] ${row.id} ${row.name} (${row.duration_ms}ms)${suffix}`);
    if (row.error) console.log(row.error);
    return row;
  }

  write() {
    const passed = this.rows.filter(row => row.status === 'PASS').length;
    const failed = this.rows.filter(row => row.status === 'FAIL').length;
    const skipped = this.rows.filter(row => row.status !== 'PASS' && row.status !== 'FAIL').length;
    const summary = {
      total: this.rows.length,
      passed,
      failed,
      skipped,
      rows: this.rows
    };
    fs.writeFileSync(path.join(this.dir, 'report.json'), JSON.stringify(summary, null, 2));
    const lines = [
      `packaged E2E: ${passed} passed, ${failed} failed, ${skipped} skipped`,
      '',
      ...this.rows.map(
        row =>
          `${row.status.padEnd(5)} ${row.id.padEnd(4)} ${row.name}` +
          (row.reason ? ` — ${row.reason}` : '') +
          (row.screenshot ? ` (${row.screenshot})` : '')
      )
    ];
    fs.writeFileSync(path.join(this.dir, 'summary.txt'), lines.join('\n') + '\n');
    console.log(lines.join('\n'));
    return { passed, failed, skipped };
  }
}
