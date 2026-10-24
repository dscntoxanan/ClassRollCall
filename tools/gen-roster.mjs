// 把 src/roster.txt（UTF-8）编译成 src/roster.h
// 每行格式：`姓名` 或 `姓名,权重`（权重缺省 1.0，>=0，0 表示出厂就不抽）
// 行号即编号：第 1 行 = 1 号。新增请追加到末尾。
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const raw = readFileSync(join(root, 'src', 'roster.txt'), 'utf8');
const rows = raw
  .split(/\r?\n/)
  .map((s) => s.trim())
  .filter((s) => s.length > 0 && !s.startsWith('#'))
  .map((line) => {
    const i = line.lastIndexOf(',');
    if (i < 0) return { name: line, weight: 1 };
    const name = line.slice(0, i).trim();
    const num = Number(line.slice(i + 1).trim());
    if (!name) throw new Error('空名字: ' + line);
    if (!Number.isFinite(num) || num < 0 || num > 1000) throw new Error('权重不合法: ' + line);
    return { name, weight: num };
  });

if (rows.length === 0) throw new Error('roster.txt 是空的');
if (rows.length > 128) throw new Error('名单最多 128 人（与客户端 ROSTER_MAX 一致）');
for (const r of rows) {
  if (/["\\|]/.test(r.name)) throw new Error('名字里不能有引号/反斜杠/竖线: ' + r.name);
  if (r.name.length > 12) throw new Error('名字太长: ' + r.name);
}

const esc = (s) => s.replace(/[\u0080-\uFFFF]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
const num = (v) => (Number.isInteger(v) ? v.toFixed(1) : String(v));

let out = '/* 自动生成：node tools/gen-roster.mjs —— 不要手改本文件，改 src/roster.txt */\n';
out += '#ifndef ROSTER_H\n#define ROSTER_H\n\n';
out += `#define ROSTER_COUNT ${rows.length}\n\n`;
out += 'static const wchar_t *const kRosterNames[ROSTER_COUNT] = {\n';
rows.forEach((r, i) => {
  out += `    L"${esc(r.name)}", /* ${String(i + 1).padStart(2, '0')} */\n`;
});
out += '};\n\n';
out += '/* 出厂默认权重（服务器同步到的会覆盖它） */\n';
out += 'static const double kRosterWeights[ROSTER_COUNT] = {\n';
rows.forEach((r, i) => {
  out += `    ${num(r.weight)}, /* ${String(i + 1).padStart(2, '0')} ${r.name} */\n`;
});
out += '};\n\n#endif\n';

writeFileSync(join(root, 'src', 'roster.h'), out, 'utf8');
console.log(`roster.h: ${rows.length} students, ${rows.filter((r) => r.weight !== 1).length} with custom weight`);
