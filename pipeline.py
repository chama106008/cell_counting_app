# pipeline.py
import os, glob, math, csv, time
import numpy as np
import tifffile as tiff
from natsort import natsorted
from cellpose import models, io
import re
import matplotlib.pyplot as plt
from skimage.measure import regionprops
from cellpose.utils import masks_to_outlines

# logger関係
import logging
from typing import Callable, List

# GUI
from PyQt5 import QtWidgets as _qtw
from PyQt5 import QtCore   as _qtc

# count出力
import json, tempfile

#キャッシュクリア
import gc, psutil
import torch

#single test
from PyQt5 import QtWidgets as _qtw, QtCore as _qtc, QtGui as _qtg


# 画像ファイル認識
# --- 画像拡張子とユーティリティ ---
IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")

def is_image_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS

def glob_images(dirpath: str):
    # 拡張子ごとに拾って自然順で結合
    files = []
    for ext in IMAGE_EXTS:
        files += glob.glob(os.path.join(dirpath, f"*{ext}"))
    return natsorted(files)



# count随時出力
def _append_csv_row_safely(csv_path: str, header: list[str] | None, row: list):
    """CSVが無ければヘッダを作ってから1行追記。flush + fsync で落ちても残る"""
    first = not os.path.exists(csv_path)
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if first and header:
            w.writerow(header)
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())

def _atomic_write_json(path: str, obj: dict):
    """一時ファイル→os.replace で原子的に更新"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d = json.dumps(obj, ensure_ascii=False, indent=2)
    dirn = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dirn, delete=False, encoding="utf-8", newline="") as tmp:
        tmp.write(d)
        tmp.flush(); os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def rebuild_wide_from_long(long_csv: str, out_path: str):
    """long CSV -> wide CSV を後から再生成できるように"""
    import pandas as pd
    df = pd.read_csv(long_csv, header=None)
    # 期待ヘッダ: ("Field","Index","Filename","Count")
    if df.shape[1] == 4:
        df.columns = ["Field","Index","Filename","Count"]
    w = df.pivot_table(index="Field", columns="Filename", values="Count", aggfunc="first")
    w.reset_index().to_csv(out_path, index=False)


# 1) 共通ロガー
logger = logging.getLogger("cellapp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()  # コンソール（デバッグ時用）
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_h)

# 2) GUI への配信ハンドラ（複数タブ対応）
_gui_handlers: List[logging.Handler] = []

class _GuiHandler(logging.Handler):
    """emit() で与えられたコールバックに文字列を流すハンドラ"""
    def __init__(self, sink: Callable[[str], None]):
        super().__init__()
        self._sink = sink
        self.setFormatter(logging.Formatter("%(message)s"))  # GUIは簡潔に

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._sink(msg)
        except Exception:
            pass

def add_gui_log_sink(sink: Callable[[str], None]) -> None:
    """タブごとに複数登録OK"""
    h = _GuiHandler(sink)
    _gui_handlers.append(h)
    logger.addHandler(h)

def clear_gui_log_sinks() -> None:
    """不要なら外す（任意）"""
    global _gui_handlers
    for h in _gui_handlers:
        logger.removeHandler(h)
    _gui_handlers = []

# 3) 使いやすいショートカット
def log_info(msg: str) -> None:  logger.info(msg)
def log_warn(msg: str) -> None:  logger.warning(msg)
def log_err(msg: str) -> None:   logger.error(msg)

#single test
def single_test_handler(
    model,
    *,
    scale: float,
    diameter_px: int,
    invert: bool,
    cellprob_threshold: float,
    flow_threshold: float,
    min_diam_frac: float,
    circularity_max: float,
    out_root: str,
    parent: "_qtw.QWidget | None" = None,
) -> None:
    """1枚だけ選んでカウント→オーバーレイPNGを即ポップアップ表示（CSVなし）"""
    try:
        # 1) ファイル選択
        path, _ = _qtw.QFileDialog.getOpenFileName(
            parent, "検証用の画像を1枚選択", "",
            "Images (*.tif *.tiff *.png *.jpg *.jpeg);;All Files (*)"
        )
        if not path:
            return

        # 2) セルカウント（オーバーレイPNGは count_one_image が保存）
        logger.info(f"[single] 画像: {path}")
        vis_dir = os.path.join(out_root or ".", "viz", "_single")
        n_cells, overlay_path = count_one_image(
            model, path,
            out_dir_png=vis_dir,
            scale=scale,
            diameter_px=diameter_px,
            invert=invert,
            cellprob_threshold=cellprob_threshold,
            flow_threshold=flow_threshold,
            min_diam_frac=min_diam_frac,
            circularity_max=circularity_max,
        )
        logger.info(f"[single] count = {n_cells}")

        # 3) プレビュー（GUIスレッドで安全に表示）
        if overlay_path and os.path.exists(overlay_path):

            def _show_preview():
                try:
                    dlg = _qtw.QDialog(parent)
                    dlg.setWindowTitle(f"Overlay: {os.path.basename(path)}")
                    label = _qtw.QLabel(dlg)
                    pix = _qtg.QPixmap(overlay_path)
                    label.setPixmap(pix)
                    label.setScaledContents(True)
                    lay = _qtw.QVBoxLayout(dlg)
                    lay.addWidget(label)
                    dlg.resize(650, 650)
                    dlg.exec_()
                except Exception as e:
                    logger.error(f"[single][preview][error] {e}", exc_info=True)

            # メインGUIスレッドに委譲
            _qtc.QTimer.singleShot(0, _show_preview)

    except Exception as e:
        logger.error(f"[single][error] {e}", exc_info=True)



# ------Z project系------
def _read_with_tifffile(path):
    return tiff.imread(path)

def _read_with_pillow(path):
    from PIL import Image
    im = Image.open(path)
    # 16bit/8bitどちらも対応しつつグレースケールへ
    if im.mode not in ("I;16", "I;16B", "I;16L", "L"):
        im = im.convert("L")
    return np.array(im)

def _read_with_cv2(path):
    import cv2
    arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise IOError("cv2.imread returned None")
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    return arr

def read_gray_safe(path: str) -> np.ndarray:
    """壊れたLZWなどにも耐える多段フォールバック読み込み→2D ndarray"""
    last_err = None
    for reader in (_read_with_tifffile, _read_with_pillow, _read_with_cv2):
        try:
            arr = reader(path)
            # 2D化（カラーやZが来ても平均で2Dへ）
            if arr.ndim == 2:
                return arr
            y, x = arr.shape[-2], arr.shape[-1]
            pre  = int(np.prod(arr.shape[:-2]))
            arr2 = arr.reshape(pre, y, x).mean(axis=0)
            return arr2.astype(arr.dtype)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Failed to read image: {path}  ({last_err})")

def project_stack_with_log(image_paths, method: str = "max", min_valid: int | None = None):
    """
    返り値:
      proj      : 2D ndarray（成功時）
      good_list : 正常に読めたファイルのパス一覧
      skip_list : 読めずにスキップした (path, reason) の一覧
    """
    method = method.lower()
    if method not in {"max","average","median"}:
        raise ValueError("method must be 'max' | 'average' | 'median'")
    if min_valid is None:
        min_valid = 1 if method == "max" else 2

    good = []
    good_paths = []
    skip_list = []

    for p in sorted(image_paths):
        try:
            img = read_gray_safe(p)     # ← 以前お渡ししたフォールバック付き関数
            good.append(img)
            good_paths.append(p)
        except Exception as e:
            skip_list.append((p, str(e)))

    if len(good) == 0:
        # 1枚も読めなければ完全失敗
        raise RuntimeError(f"No usable frames. Total={len(image_paths)}, skipped={len(skip_list)}")

    # サイズ統一（不一致もスキップに回す）
    H, W = good[0].shape
    good2, good_paths2 = [], []
    for img, path in zip(good, good_paths):
        if img.shape == (H, W):
            good2.append(img); good_paths2.append(path)
        else:
            skip_list.append((path, f"Size mismatch: got {img.shape}, expected {(H,W)}"))

    if len(good2) < min_valid:
        raise RuntimeError(f"Too few usable frames after filtering: {len(good2)} (need >= {min_valid})")

    dtype = good2[0].dtype
    if method == "max":
        proj = good2[0].copy()
        for g in good2[1:]:
            proj = np.maximum(proj, g)
    elif method == "average":
        acc = good2[0].astype(np.float64)
        for g in good2[1:]:
            acc += g
        avg = acc / len(good2)
        proj = np.clip(np.rint(avg), np.iinfo(dtype).min, np.iinfo(dtype).max).astype(dtype)
    else:  # median
        stack = np.stack(good2, axis=0)
        med = np.median(stack, axis=0)
        proj = np.clip(np.rint(med), np.iinfo(dtype).min, np.iinfo(dtype).max).astype(dtype)

    return proj, good_paths2, skip_list


# ---- 末端スタックフォルダ検出（深さ制限あり：max_depth） ----
def find_leaf_stack_dirs(root: str, max_depth: int = 5):
    root = os.path.abspath(root)
    image_dirs = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirpath = os.path.abspath(dirpath)
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1  # root直下を1
        if depth >= max_depth:
            dirnames[:] = []  # これ以上潜らない
        if any(is_image_file(f) for f in filenames):
            image_dirs.add(dirpath)
    if not image_dirs:
        return []
    image_dirs = sorted(image_dirs, key=lambda p: (p.count(os.sep), p))
    image_set  = set(image_dirs)
    leaf = []
    for d in image_dirs:
        has_child = any(o != d and o.startswith(d + os.sep) for o in image_set)
        if not has_child:
            rel = os.path.relpath(d, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth <= max_depth:
                leaf.append(d)
    return leaf


#skip アリの修正版
def process_tree(parent: str, out_root: str, method: str = "max", max_depth: int = 5,
                 overwrite: bool = False,
                 failed_log_path: str | None = None,
                 skipped_log_path: str | None = None,
                 min_valid: int | None = None):
    """
    - 失敗フォルダは failed_log_path（TSV）
    - フレーム単位のスキップは skipped_log_path（TSV）
    - 成功したときだけ出力用サブフォルダを作成
    """
    leaf_dirs = find_leaf_stack_dirs(parent, max_depth=max_depth)
    if not leaf_dirs:
        raise RuntimeError(f"No leaf stack dirs found under: {parent} (max_depth={max_depth})")

    os.makedirs(out_root, exist_ok=True)
    failed = []   # (stack_dir, reason)
    skipped = []  # (stack_dir, file_path, reason)
    results, skipped_stacks = [], 0

    for stack_dir in sorted(leaf_dirs):
        time_name  = os.path.basename(os.path.normpath(stack_dir))
        field_name = os.path.basename(os.path.normpath(os.path.dirname(stack_dir)))
        tifs = glob_images(stack_dir)
        if not tifs:
            continue

        out_path = os.path.join(out_root, field_name, f"{time_name}_{method}.tif")
        if (not overwrite) and os.path.exists(out_path):
            skipped_stacks += 1
            continue

        try:
            # ★ ここで投影（処理の本体はこの1行）
            proj, good_paths, skip_list = project_stack_with_log(tifs, method=method, min_valid=min_valid)

            # スキップされたフレームをログに蓄積
            for fp, rsn in skip_list:
                skipped.append((stack_dir, fp, rsn))

            # ★ 成功してからフォルダ作成→保存
            field_out = os.path.join(out_root, field_name)
            os.makedirs(field_out, exist_ok=True)
            tiff.imwrite(out_path, proj)

            results.append(out_path)

        except Exception as e:
            failed.append((stack_dir, str(e)))

    # --- ログ出力 ---
    if failed_log_path is None:
        failed_log_path = os.path.join(out_root, "_failed_stacks.tsv")
    if skipped_log_path is None:
        skipped_log_path = os.path.join(out_root, "_skipped_frames.tsv")

    if failed:
        with open(failed_log_path, "w", encoding="utf-8") as fw:
            fw.write("stack_dir\treason\n")
            for d, msg in failed:
                fw.write(f"{d}\t{msg}\n")

    if skipped:
        with open(skipped_log_path, "w", encoding="utf-8") as fw:
            fw.write("stack_dir\tfile_path\treason\n")
            for d, fp, msg in skipped:
                fw.write(f"{d}\t{fp}\t{msg}\n")

    #print(f"[process_tree] wrote={len(results)}  skipped_stacks={skipped_stacks}  "
    #      f"failed_stacks={len(failed)}  skipped_frames={len(skipped)}")
    #if failed:print("  失敗ログ:", failed_log_path)
    #if skipped:print("  スキップフレーム一覧:", skipped_log_path)
    
    # 置換後:
    logger.info(f"[process_tree] wrote={len(results)}  skipped_stacks={skipped_stacks}  "
            f"failed_stacks={len(failed)}  skipped_frames={len(skipped)}")
    if failed:
        logger.info(f"  失敗ログ: {failed_log_path}")
    if skipped:
        logger.info(f"  スキップフレーム一覧: {skipped_log_path}")

    return results



# --- 自然順ソート（T0001, T0002 ... を正しく並べる） ---
_num = re.compile(r'(\d+)')
def natsort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in _num.split(s)]

# --- 視野フォルダ列挙（柔軟版：親フォルダにも、視野フォルダ自身にも対応） ---
def list_field_dirs_flexible(projected_root: str):
    root = os.path.abspath(projected_root)
    # root 自身に .tif があれば単一視野とみなす
    has_tif = any(is_image_file(f)
                  for f in os.listdir(root) if os.path.isfile(os.path.join(root,f)))
    if has_tif:
        return [root]
    # 直下サブフォルダで .tif を含むものを視野とみなす
    subs = [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]
    fields = []
    for d in sorted(subs, key=natsort_key):
        if any(is_image_file(f)
               for f in os.listdir(d) if os.path.isfile(os.path.join(d,f))):
            fields.append(d)
    return fields

# --- v2/v3/v4 互換の eval ラッパ（戻り値の長さ違いに対応） ---
def eval_cp(model, img, **kw):
    out = model.eval(img, **kw)
    if len(out) == 3:
        masks, flows, styles = out; diams = None
    elif len(out) == 4:
        masks, flows, styles, diams = out
    else:
        raise RuntimeError(f"Unexpected return length: {len(out)}")
    return masks, flows, styles, diams


#単一試行
def count_one_image(model, path, out_dir_png=None,
                    scale=0.7, diameter_px=30, invert=True,
                    cellprob_threshold=0.15,
                    flow_threshold=0.4,
                    min_diam_frac=0.25,
                    circularity_max=0.80): #scaleでサイズ縮小、検証なら0.7程度でも可
    """
    戻り値: (n_cells, overlay_path or None)
    """
    from cellpose import io
    import cv2, os
    img = io.imread(path)
    base = img if img.ndim==2 else img[...,0]
    h, w = base.shape

    small = cv2.resize(base, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)

    D = diameter_px * scale
    masks, flows, styles, diams = eval_cp(
        model, small,
        diameter=diameter_px*scale,
        channel_axis=None, do_3D=False,
        invert=invert, resample=False, batch_size=1, tile_overlap=0.1,
        cellprob_threshold=cellprob_threshold,   # ← 引数を使う
        flow_threshold=flow_threshold,           # ← 引数を使う
        min_size=int(np.pi*(0.35*D/2)**2),
    )

    # 期待直径を基準に閾値設定してクリーニング
    clean = filter_masks_by_geometry(
        masks,
        min_diam_px=min_diam_frac * D,    # ← 引数を使う
        circularity_min=0.0,
        circularity_max=circularity_max,  # ← 引数を使う
    )
    n_cells = int(clean.max())

    overlay_path = None
    if out_dir_png and clean is not None:
        os.makedirs(out_dir_png, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        overlay_path = os.path.join(out_dir_png, f"{stem}_overlay.png")
        overlay_png_fast(small, clean, overlay_path)

    return n_cells, overlay_path

#マスクの後処理用フィルタ
def filter_masks_by_geometry(masks: np.ndarray,
                             min_diam_px: float,
                             max_diam_px: float | None = None,
                             circularity_min: float = 0.0,
                             circularity_max: float = 0.0,
                             solidity_min: float = 0.0) -> np.ndarray:
    """
    circularity = 4πA / P^2 （1に近いほど円）
    solidity    = A / A_convex （1に近いほど凸）
    """
    if masks is None or masks.max() == 0:
        return masks

    lbl = masks.astype(np.int32)
    out = np.zeros_like(lbl)
    cur_id = 1

    # 面積の下限・上限（px）
    def area_from_d(d): return np.pi * (d/2.0)**2
    min_area = area_from_d(min_diam_px)
    max_area = area_from_d(max_diam_px) if max_diam_px else None

    for rp in regionprops(lbl):
        A = rp.area
        P = rp.perimeter if rp.perimeter > 0 else 1.0
        circ = 4.0 * np.pi * A / (P*P)     # 0〜1
        solid = rp.solidity if rp.solidity is not None else 0.0

        if A < min_area:       # 小さすぎるゴミ
            continue
        if max_area and A > max_area:  # 巨大な凝集塊
            continue
        if circ < circularity_min:
            continue
        if circ > circularity_max:
            continue
        if solid < solidity_min:
            continue

        out[lbl == rp.label] = cur_id
        cur_id += 1

    return out

"""
# --- オーバーレイPNGの保存 ---
def overlay_png(img2d, masks, out_png):
    fig = plt.figure(figsize=(6,6))
    ax = plt.gca()
    ax.imshow(img2d, cmap="gray")
    if masks is not None and masks.max() > 0:
        outlines = masks_to_outlines(masks)
        ys, xs = np.nonzero(outlines)
        ax.scatter(xs, ys, s=0.2)
    ax.set_axis_off()
    fig.tight_layout(pad=0)
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
"""


# === 置換：高速＆安全なオーバーレイ保存（Matplotlib不使用） ===
def overlay_png_fast(gray2d, masks, out_png):
    import cv2, numpy as np, os
    img = gray2d
    if img.dtype != np.uint8:
        lo, hi = float(img.min()), float(img.max()) or 1.0
        img8 = ((img - lo) * (255.0/(hi-lo))).clip(0,255).astype(np.uint8)
    else:
        img8 = img
    bgr = cv2.cvtColor(img8, cv2.COLOR_GRAY2BGR)

    if masks is not None and masks.max() > 0:
        lbl = masks.astype(np.int32)
        # ラベル毎に境界線を描画（ピクセル差分で軽量）
        edge = ((np.roll(lbl,-1,0)!=lbl)|(np.roll(lbl,1,0)!=lbl)|
                (np.roll(lbl,-1,1)!=lbl)|(np.roll(lbl,1,1)!=lbl)) & (lbl>0)
        ey, ex = np.where(edge)
        bgr[ey, ex] = (0, 255, 255)  # 黄ライン

    cv2.imwrite(out_png, bgr)



# ==== ここから「オーケストラ関数」セット ====
import os, glob, csv, time
from natsort import natsorted
from cellpose import models

# 依存（このファイルに既にあるはずの関数/インポート）
# - list_field_dirs_flexible
# - count_one_image
# ほか、下のコメントにある import が未定義なら足してください

def init_model(use_gpu: bool = True):
    """
    Cellpose v4 のモデルを初期化（重いので1回だけ）
    GUI起動時に1度だけ呼ぶ想定
    """
    try:
        model = models.CellposeModel(gpu=use_gpu)
    except Exception:
        # GPU失敗時はCPUでフォールバック
        model = models.CellposeModel(gpu=False)
    return model


# ==== 逐次保存cellcount ====
def run_cellcount(
    model,
    projected_root: str,
    out_dir: str,
    *,
    scale: float = 0.7,
    diameter_px: int = 30,
    invert: bool = True,
    cellprob_threshold: float = 0.15,
    flow_threshold: float = 0.4,
    min_diam_frac: float = 0.25,
    circularity_max: float = 0.80,
    save_overlay: bool = True,
):
    os.makedirs(out_dir, exist_ok=True)
    fields = list_field_dirs_flexible(projected_root)

    # 逐次保存する long CSV（最優先で残す）
    long_csv = os.path.join(out_dir, "cell_counts_long.csv")
    long_header = ["Field","Index","Filename","Count"]

    # 視野ごとの進捗チェックポイント
    ckpt_dir = os.path.join(out_dir, "_checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)

    t_global0 = time.perf_counter()
    for f_idx, fdir in enumerate(fields, 1):
        field_name = os.path.basename(os.path.normpath(fdir))
        tifs = glob_images(fdir)
        if not tifs:
            continue

        # 視野ごと viz 出力
        vis_dir = os.path.join(out_dir, "viz", field_name) if save_overlay else None

        # チェックポイントを読み込んでスキップ
        ckpt_path = os.path.join(ckpt_dir, f"{field_name}.json")
        done_set: set[str] = set()
        start_index = 0
        if os.path.exists(ckpt_path):
            try:
                meta = json.load(open(ckpt_path, "r", encoding="utf-8"))
                done_set = set(meta.get("done_files", []))
                start_index = int(meta.get("next_index", 0))
            except Exception:
                pass

        # ファイル名ベースでスキップ（viz が既にある場合もスキップしたいなら、その判定を追記してOK）
        images_to_run = []
        for i, p in enumerate(tifs):
            fn = os.path.basename(p)
            if i < start_index or fn in done_set:
                continue
            images_to_run.append((i, p))

        if not images_to_run:
            logger.info(f"[resume] {field_name}: 既に完了")
            continue

        logger.info(f"[start] {field_name}: {len(images_to_run)}/{len(tifs)} 枚を処理")

        t0 = time.perf_counter()
        for i, p in images_to_run:
            # ---- カウント ----
            n, _ov = count_one_image(
                model, p, out_dir_png=vis_dir,
                scale=scale, diameter_px=diameter_px, invert=invert,
                cellprob_threshold=cellprob_threshold,
                flow_threshold=flow_threshold,
                min_diam_frac=min_diam_frac,
                circularity_max=circularity_max,
            )

            # ---- long CSV に1行追記（即 flush+fsync）----
            _append_csv_row_safely(long_csv, long_header, [field_name, i, os.path.basename(p), n])

            # ---- チェックポイント更新 ----
            done_set.add(os.path.basename(p))
            _atomic_write_json(ckpt_path, {
                "field": field_name,
                "next_index": i+1,
                "done_files": sorted(list(done_set), key=natsort_key),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

            # 進捗ログ（控えめに）
            if ((i+1) % 10 == 0) or ((i+1) == len(tifs)):
                logger.info(f"[{f_idx}/{len(fields)}] {field_name}: {i+1}/{len(tifs)} -> last={n}")
            # (メモリクリア)
            if ((i+1) % 20) == 0:
                try:
                    gc.collect()
                    # CUDA のキャッシュも軽く掃除（メモリ返却ではないが断片化予防）
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    rss = psutil.Process(os.getpid()).memory_info().rss / (1024**2)
                    logger.info(f"[mem] RSS ~ {rss:.1f} MB")
                except Exception:
                    pass


        dt = time.perf_counter() - t0
        logger.info(f"[done] {field_name}: {len(images_to_run)}枚  {dt:.1f}s  avg {dt/max(1,len(images_to_run)):.2f}s/枚")

    # wide は “後から再構築できる”ので、最後に作る or 別ボタンで再生成
    wide_csv = os.path.join(out_dir, "cell_counts_wide.csv")
    try:
        rebuild_wide_from_long(long_csv, wide_csv)
        logger.info(f"CSV: LONG={os.path.abspath(long_csv)}  WIDE={os.path.abspath(wide_csv)}")
    except Exception as e:
        logger.warning(f"[wide build skipped] {e}  long={long_csv}")

    logger.info(f"[ALL DONE] {len(fields)}視野, {(time.perf_counter()-t_global0):.1f}s")
    return wide_csv, long_csv


# （任意）Z投影のラッパ：あなたの process_tree(...) を1発呼ぶだけ
def run_zproject(parent_dir: str, out_root: str, method: str = "max",
                 max_depth: int = 5, overwrite: bool = False):
    """
    既に作ってある process_tree(...) を呼び出してOK。
    ここでは関数名だけ合わせておく。
    """
    # from your_module import process_tree  # もし別ファイルならこう import
    # いま同じファイル内にあるならそのまま使える
    results = process_tree(parent_dir, out_root, method=method,
                           max_depth=max_depth, overwrite=overwrite)
    #出力はターミナルからUIに差し替え
    #print(f"[Z-Project] wrote={len(results)}")
    logger.info(f"[Z-Project] wrote={len(results)}")

    return len(results)


