"use client";
import {
  Chart as ChartJS, CategoryScale, LinearScale, PointElement,
  LineElement, Tooltip, Filler,
} from "chart.js";
import { Line } from "react-chartjs-2";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Filler);

export default function EquityChart({
  points, labels, height = 220,
}: { points: number[]; labels?: string[]; height?: number }) {
  if (!points.length)
    return <div className="text-slate-500 text-sm py-8 text-center">no equity data yet</div>;
  const up = points[points.length - 1] >= points[0];
  const color = up ? "#34d399" : "#f87171";
  return (
    <div style={{ height }}>
      <Line
        data={{
          labels: labels ?? points.map((_, i) => String(i)),
          datasets: [{
            data: points,
            borderColor: color,
            backgroundColor: color + "22",
            fill: true,
            pointRadius: 0,
            borderWidth: 1.5,
            tension: 0.15,
          }],
        }}
        options={{
          responsive: true,
          maintainAspectRatio: false,
          plugins: { tooltip: { intersect: false, mode: "index" } },
          scales: {
            x: { display: false },
            y: {
              grid: { color: "#1e2a3a" },
              ticks: { color: "#64748b", font: { size: 10 } },
            },
          },
        }}
      />
    </div>
  );
}
