import sys
import cv2
import numpy as np

a = cv2.imread(sys.argv[1])
b = cv2.imread(sys.argv[2])
print("shape", a.shape, b.shape)
diff = cv2.absdiff(a, b)
gray = diff.max(axis=2)
changed = gray > 12
print("changed pixels: %d (%.2f%%)" % (changed.sum(), 100.0 * changed.sum() / changed.size))

h, w = gray.shape
rows = (changed.sum(axis=1) > 3).astype(int)
cols = (changed.sum(axis=0) > 3).astype(int)
if rows.any():
    print("changed rows  %d..%d" % (np.argmax(rows), h - np.argmax(rows[::-1]) - 1))
if cols.any():
    print("changed cols  %d..%d" % (np.argmax(cols), w - np.argmax(cols[::-1]) - 1))

# report changed ratio per quadrant band to locate the live area
for name, sl in [("top", slice(0, h // 4)), ("upper", slice(h // 4, h // 2)),
                 ("lower", slice(h // 2, 3 * h // 4)), ("bottom", slice(3 * h // 4, h))]:
    print("  %-7s changed %.2f%%" % (name, 100.0 * changed[sl].sum() / changed[sl].size))
