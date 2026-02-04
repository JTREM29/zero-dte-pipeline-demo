import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(10, 5), dpi=150)
ax.set_title("WORKER SMOKE")
ax.text(0.5, 0.5, "Dell Worker OK", ha="center", va="center", fontsize=28, alpha=0.25, transform=ax.transAxes)
ax.set_xlabel("Strike")
ax.set_ylabel("Open interest")

import sys
out_path = "worker_smoke_oi.png"
fig.savefig(out_path, format="png", bbox_inches="tight")
plt.close(fig)
print("WROTE:", out_path)
