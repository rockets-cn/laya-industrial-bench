// 光门/孪生动画行为桩 (DOM 打桩, 驱动真实内嵌脚本按时间线断言)
// 用法: node tests/gate_anim_dom_test.js <gate_layout_anim.svg>
// 覆盖: 直通不停推料点 / busy→推料点(非 G[i]-90) / sim armed→带首全旅程 /
//       推时停带(beltT 冻结) / wait→下一件带首 / setSrc(device) 清全部旅程 /
//       相机识别显示 / 光门随物品亮 / 无指令推杆绝不动作
'use strict';
const fs = require('fs');
const svg = fs.readFileSync(process.argv[2], 'utf8');
const m = svg.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('未找到内嵌脚本'); process.exit(1); }
// XML 内联安全约束
if (/&&/.test(m[1])) { console.error('脚本含 && (XML 内联不安全)'); process.exit(1); }
if (/<(?!\/?[a-zA-Z])/.test(m[1].replace(/<\//g, ''))) { console.error('脚本含裸 <'); process.exit(1); }

// ---- DOM 打桩 ----
const els = new Map();
function makeEl(id) {
  const a = {};
  return {
    id, _a: a, textContent: '',
    setAttribute(k, v) { a[k] = String(v); },
    getAttribute(k) { return (k in a) ? a[k] : null; },
  };
}
const documentStub = { getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); } };
const windowStub = { matchMedia: () => null, __gla: null };
const rafStub = (cb) => cb;

new Function('document', 'window', 'requestAnimationFrame', m[1])(documentStub, windowStub, rafStub);
const g = () => windowStub.__gla;
if (!g) { console.error('window.__gla 未导出'); process.exit(1); }

const el = (id) => documentStub.getElementById(id);
const item = (n) => el('gla-obj-' + n);
const parsePos = (n) => {
  const t = item(n).getAttribute('transform') || '';
  const mm = t.match(/translate\(([-0-9.]+),([0-9.]+)/);
  return mm ? { x: parseFloat(mm[1]), y: parseFloat(mm[2]) } : null;
};
const rodY2 = (n) => parseFloat(el('gla-rod-' + n).getAttribute('y2'));
const beltTx = () => parseFloat(el('gla-treads').getAttribute('transform').match(/translate\(([-0-9.]+)/)[1]);
const op = (n) => parseFloat(item(n).getAttribute('opacity'));

let failures = 0;
function check(name, cond) {
  console.log((cond ? '  PASS ' : '  FAIL ') + name);
  if (!cond) failures++;
}

// ---- 时间线 (now = 绝对 epoch ms; 事件 ts 必在过去) ----
let nowMs = Date.now();
function frames(n, dt) { for (let i = 0; i < n; i++) g().twinFrame(nowMs += dt); }

const R = (ago) => ({ state: 'ready', ts_ms: nowMs - ago });
const twinBase = () => ({
  online: true, belt_mps: 1, belt_on: true, t0: 0, active_job: null, status: '',
  sorters: [R(5000), R(5000), R(5000)],
});

// boot: 全 ready 基线 (事件 ts 在 5s 前)
g().setTwin(twinBase());
frames(2, 100);

// ===== S1: sim 源 armed 事件 → 全旅程从带首 X0=40 =====
g().setJob([{ yard: 2, material: 'pear', label: '梨 × 2', color: '#a3d977', qty: 2 }]);
const s1 = twinBase(); s1.sorters[1] = { state: 'armed', ts_ms: nowMs - 50 };
g().setTwin(s1);
frames(1, 100);
let p = parsePos(2);
check('S1 armed→任务旅程出现在带首 X0=40', p && Math.abs(p.x - 40) < 0.5 && p.y === 400);
check('S1 推杆未动作 (y2=438)', rodY2(2) === 438.0);

// 相机识别: 过 CAMX=150 时间窗内某帧显示任务物料名
let camSawTask = false;
for (let i = 0; i < 10; i++) {
  g().twinFrame(nowMs += 100);
  if (el('gla-visiong').getAttribute('opacity') === '1' && el('gla-vision').textContent.indexOf('梨') >= 0) camSawTask = true;
  if (rodY2(2) < 438.0) break; // 已到推料点
}
check('S1 相机显示任务物料识别 (' + el('gla-vision').textContent + ')', camSawTask);

// 走到推料点 (X0→P[1]=800 ≈ 5.43s), 途中光门 1/2 亮
let gate1Lit = false, gate2Lit = false, reachedPush = false;
for (let i = 0; i < 80 && !reachedPush; i++) {
  g().twinFrame(nowMs += 100);
  if (parseFloat(el('gla-gate-1').getAttribute('opacity')) > 0.5) gate1Lit = true;
  if (parseFloat(el('gla-gate-2').getAttribute('opacity')) > 0.5) gate2Lit = true;
  if (rodY2(2) < 438.0) reachedPush = true;
}
check('S1 物品经过光门 1/2 时虚线亮', gate1Lit && gate2Lit);
check('S1 到推料点 P[1]=800 停带', reachedPush && Math.abs(parsePos(2).x - 800) < 0.5);
check('S1 推杆正在伸出 (y2 介于 402~438)', rodY2(2) > 402.5 && rodY2(2) < 438);

// 推时停带: beltT 冻结 → 带纹 tx 不动, 物品钉在 800
const tx0 = beltTx();
frames(3, 100);
check('S1 armed 期间带纹冻结 (tx 不变)', Math.abs(beltTx() - tx0) < 0.05);
check('S1 物品钉在推料点', Math.abs(parsePos(2).x - 800) < 0.5);

// busy 滑入 (450+315ms 后开始滑)
frames(8, 100);
p = parsePos(2);
check('S1 busy 物品滑入料道 (y>400)', p && p.y > 400.5);

// 滑入完成 (450+900ms) → wait 700ms → 第 2 件带首装料
frames(11, 100); // 累计至 tArmed+2300ms (wait→travel 切换帧次帧才落位)
p = parsePos(2);
check('S2 第 2 件从带首 X0 重新装料', p && Math.abs(p.x - 40) < 60 && p.y === 400);

// ===== S3: busy 事件 (错过 armed / 旧固件) → 物品在推料点 P[i], 不是 G[i]-90 =====
const s3 = twinBase(); s3.sorters[0] = { state: 'busy', ts_ms: nowMs - 50 };
g().setTwin(s3);
frames(1, 100);
p = parsePos(1);
check('S3 busy→物品在推料点 P[0]=530 (非 G[0]-90=290)', p && Math.abs(p.x - 530) < 0.5 && p.y === 400);
frames(2, 100);
check('S3 推杆随即伸出 (y2<438)', rodY2(1) < 438.0);
const tx1 = beltTx();
frames(2, 100);
check('S3 推时带纹冻结', Math.abs(beltTx() - tx1) < 0.05);
check('S3 相机显示识别 (' + el('gla-vision').textContent + ')', el('gla-visiong').getAttribute('opacity') === '1');

// ===== S4: 切真实源 → 带面清空 (事件驱动, 不虚构物品) =====
g().setSrc('device');
frames(12, 100); // legacy busy 滑出窗口走完
check('S4 device 源清空全部旅程物品 (op=0)', op(1) === 0 && op(2) === 0 && op(3) === 0);

// ===== S5: sim 源环境供血 — 直通走满到带尾, 推杆绝不动作 =====
g().setSrc('sim');
g().setJob([]); // 无任务
g().setTwin(twinBase()); // 新轮询: 全 ready
const before = beltTx();
let rodMoved = false, peakX = 0;
for (let i = 0; i < 150; i++) { // 15s
  g().twinFrame(nowMs += 100);
  for (let n = 1; n <= 3; n++) {
    if (rodY2(n) !== 438.0) rodMoved = true;
    const pp = parsePos(n);
    if (pp && op(n) > 0.5 && pp.x > peakX) peakX = pp.x;
  }
}
check('S5 无指令时推杆绝不动作', !rodMoved);
check('S5 环境供血物品走满到带尾 (peakX=' + peakX.toFixed(0) + ')', peakX > 1000);
check('S5 带纹持续前进', beltTx() !== before);

console.log(failures === 0 ? 'GATE_DOM_STUB_OK' : 'GATE_DOM_STUB_FAIL(' + failures + ')');
process.exit(failures === 0 ? 0 : 1);
