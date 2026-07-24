"""Matplotlib visualizations for GraphRAG evaluation metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from evaluation.utils import METRIC_COLUMNS, ensure_dir


def plot_metric_bar(
    rows: list[dict[str, Any]],
    results_dir: str | Path,
) -> Path:
    import matplotlib.pyplot as plt

    output_dir = ensure_dir(results_dir)
    df = pd.DataFrame(rows)
    means = df[METRIC_COLUMNS].mean(numeric_only=True).dropna()

    path = output_dir / "metrics_bar.png"
    fig, ax = plt.subplots(figsize=(10, 5))
    if means.empty:
        ax.text(0.5, 0.5, "No metric scores available", ha="center", va="center")
        ax.set_axis_off()
    else:
        means.plot(kind="bar", ax=ax, color="#4f7df3")
        ax.set_ylim(0, 1)
        ax.set_ylabel("Average score")
        ax.set_title("Average GraphRAG Evaluation Metrics")
        ax.grid(axis="y", alpha=0.25)
        ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_metric_distribution(
    rows: list[dict[str, Any]],
    results_dir: str | Path,
) -> Path:
    import matplotlib.pyplot as plt

    output_dir = ensure_dir(results_dir)
    df = pd.DataFrame(rows)
    available = [
        column
        for column in METRIC_COLUMNS
        if column in df and df[column].dropna().shape[0] > 0
    ]

    path = output_dir / "metrics_distribution.png"
    fig, axes = plt.subplots(
        nrows=max(1, len(available)),
        ncols=1,
        figsize=(10, max(4, len(available) * 2.4)),
    )
    if not isinstance(axes, (list, tuple)):
        axes_list = [axes]
    else:
        axes_list = list(axes)
    if hasattr(axes, "flat"):
        axes_list = list(axes.flat)

    if not available:
        axes_list[0].text(
            0.5,
            0.5,
            "No metric scores available",
            ha="center",
            va="center",
        )
        axes_list[0].set_axis_off()
    else:
        for ax, column in zip(axes_list, available):
            df[column].dropna().plot(
                kind="hist",
                bins=10,
                range=(0, 1),
                ax=ax,
                color="#6aa9ff",
                edgecolor="#1f2937",
                alpha=0.85,
            )
            ax.set_title(column)
            ax.set_xlabel("Score")
            ax.set_ylabel("Samples")
            ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_all_metrics(rows: list[dict[str, Any]], results_dir: str | Path) -> list[Path]:
    return [
        plot_metric_bar(rows, results_dir),
        plot_metric_distribution(rows, results_dir),
    ]
