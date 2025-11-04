"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

type WaiverRow = {
  Year: number;
  Owner: string;
  Player: string;
  Pos: string;
  FirstWeek: number;
  WeeksPlayed: number;
  PointsAfterPickup: number;
  AvgPoints: number;
};

function parseTSV<T extends Record<string, any>>(text: string): T[] {
  const lines = text.trim().split(/\r?\n/);
  const headers = lines.shift()!.split("\t").map((h) => h.trim());
  return lines.map((line) => {
    const cols = line.split("\t").map((v) => v.trim());
    const obj: any = {};
    headers.forEach((h, i) => (obj[h] = cols[i]));
    return obj as T;
  });
}

const num = (x: any, fb = 0) => {
  const n = Number(String(x ?? "").replace(",", "."));
  return Number.isFinite(n) ? n : fb;
};

export default function WaiversPage() {
  const [rows, setRows] = useState<WaiverRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [year, setYear] = useState<number | "ALL">("ALL");
  const [excludeKDST, setExcludeKDST] = useState<boolean>(false);
  const [excludeQB, setExcludeQB] = useState<boolean>(false);
  const [minWeeks, setMinWeeks] = useState<number>(1);
  const [query, setQuery] = useState<string>("");

  const [sortCol, setSortCol] = useState<string>("PointsAfterPickup");
  const [sortAsc, setSortAsc] = useState<boolean>(false);

  useEffect(() => {
    fetch("/fantasy-dashboard/data/league/waivers.tsv")
      .then((r) => r.text())
      .then((txt) => {
        const r = parseTSV<Record<string, string>>(txt).map((x) => ({
          Year: num(x.Year),
          Owner: x.Owner,
          Player: x.Player,
          Pos: x.Pos,
          FirstWeek: num(x.FirstWeek),
          WeeksPlayed: num(x.WeeksPlayed),
          PointsAfterPickup: num(x.PointsAfterPickup),
          AvgPoints: num(x.AvgPoints),
        })) as WaiverRow[];
        setRows(r);
      })
      .catch(() => setError("Konnte waivers.tsv nicht laden."));
  }, []);

  const years = useMemo(() => {
    return Array.from(new Set(rows.map((r) => r.Year))).sort((a, b) => a - b);
  }, [rows]);

  const filtered = useMemo(() => {
    let list = [...rows];
    if (year !== "ALL") list = list.filter((r) => r.Year === year);
    if (excludeKDST) list = list.filter((r) => r.Pos !== "K" && r.Pos !== "DST");
    if (excludeQB) list = list.filter((r) => r.Pos !== "QB");
    if (minWeeks > 1) list = list.filter((r) => r.WeeksPlayed >= minWeeks);
    const q = query.trim().toLowerCase();
    if (q) {
      list = list.filter(
        (r) =>
          r.Player.toLowerCase().includes(q) ||
          r.Owner.toLowerCase().includes(q) ||
          r.Pos.toLowerCase().includes(q)
      );
    }
    return list;
  }, [rows, year, excludeKDST, excludeQB, minWeeks, query]);

  const sorted = useMemo(() => {
    const arr = [...filtered];
    arr.sort((a: any, b: any) => {
      const numCols = new Set([
        "Year",
        "FirstWeek",
        "WeeksPlayed",
        "PointsAfterPickup",
        "AvgPoints",
      ]);
      const A = a[sortCol];
      const B = b[sortCol];
      let cmp: number;
      if (numCols.has(sortCol)) cmp = Number(A) - Number(B);
      else cmp = String(A ?? "").localeCompare(String(B ?? ""), undefined, { numeric: true });
      return sortAsc ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortCol, sortAsc]);

  const handleSort = (key: string) => {
    if (sortCol === key) setSortAsc(!sortAsc);
    else {
      setSortCol(key);
      setSortAsc(key === "Owner" || key === "Player");
    }
  };

  const topAllTime = useMemo(() => {
    return [...rows]
      .filter((r) => {
        if (excludeKDST && (r.Pos === "K" || r.Pos === "DST")) return false;
        if (excludeQB && r.Pos === "QB") return false;
        return true;
      })
      .sort((a, b) => b.PointsAfterPickup - a.PointsAfterPickup)
      .slice(0, 20);
  }, [rows, excludeKDST, excludeQB]);

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">Best Waiver Pickups</h1>
        <Link href="/" className="text-sm underline">
          ← Back to Dashboard
        </Link>
      </div>

      {/* Filter Controls */}
      <div className="grid grid-cols-1 md:grid-cols-6 gap-3 mb-4">
        <div className="flex items-center gap-2">
          <label className="text-sm w-20">Season</label>
          <select
            className="border rounded px-2 py-1 w-full"
            value={year}
            onChange={(e) =>
              setYear(e.target.value === "ALL" ? "ALL" : Number(e.target.value))
            }
          >
            <option value="ALL">All</option>
            {years.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-sm w-20">Min Weeks</label>
          <input
            type="number"
            min={1}
            className="border rounded px-2 py-1 w-full"
            value={minWeeks}
            onChange={(e) => setMinWeeks(Number(e.target.value))}
          />
        </div>

        <div className="flex items-center gap-2">
          <label className="text-sm w-20">No K/DST</label>
          <input
            type="checkbox"
            checked={excludeKDST}
            onChange={(e) => setExcludeKDST(e.target.checked)}
          />
        </div>

        <div className="flex items-center gap-2">
          <label className="text-sm w-20">No QB</label>
          <input
            type="checkbox"
            checked={excludeQB}
            onChange={(e) => setExcludeQB(e.target.checked)}
          />
        </div>

        <div className="md:col-span-2 flex items-center gap-2">
          <label className="text-sm w-20">Search</label>
          <input
            className="border rounded px-3 py-1 w-full"
            placeholder="Player / Owner / Pos ..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      </div>

      {/* All-Time Top Box */}
      <div className="mb-6">
        <h2 className="text-lg font-semibold mb-2">
          Top Waiver Pickups (All-Time)
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full border border-gray-300 text-sm">
            <thead>
              <tr className="bg-gray-100">
                {[
                  "Year",
                  "Owner",
                  "Player",
                  "Pos",
                  "FirstWeek",
                  "WeeksPlayed",
                  "PointsAfterPickup",
                  "AvgPoints",
                ].map((col) => (
                  <th
                    key={col}
                    className="border px-2 py-1 text-center cursor-pointer hover:bg-gray-200"
                    onClick={() => handleSort(col)}
                  >
                    {col} {sortCol === col ? (sortAsc ? "▲" : "▼") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {topAllTime.map((r, i) => (
                <tr key={i} className="odd:bg-white even:bg-gray-50">
                  <td className="border px-2 py-1 text-center">{r.Year}</td>
                  <td className="border px-2 py-1 text-center">{r.Owner}</td>
                  <td className="border px-2 py-1 text-center">{r.Player}</td>
                  <td className="border px-2 py-1 text-center">{r.Pos}</td>
                  <td className="border px-2 py-1 text-center">{r.FirstWeek}</td>
                  <td className="border px-2 py-1 text-center">{r.WeeksPlayed}</td>
                  <td className="border px-2 py-1 text-center">
                    {r.PointsAfterPickup.toFixed(1)}
                  </td>
                  <td className="border px-2 py-1 text-center">
                    {r.AvgPoints.toFixed(1)}
                  </td>
                </tr>
              ))}
              {topAllTime.length === 0 && (
                <tr>
                  <td className="border px-2 py-3 text-center" colSpan={8}>
                    No data
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Full Table */}
      <h2 className="text-lg font-semibold mb-2">
        Pickups {year === "ALL" ? "(All Seasons)" : `(${year})`}
      </h2>
      {error && (
        <div className="mb-4 p-3 border border-red-300 bg-red-50 rounded text-sm">
          {error}
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full border border-gray-300 text-sm">
          <thead>
            <tr className="bg-gray-100 cursor-pointer select-none">
              {[
                { key: "Year", label: "Year" },
                { key: "Owner", label: "Owner" },
                { key: "Player", label: "Player" },
                { key: "Pos", label: "Pos" },
                { key: "FirstWeek", label: "FirstW" },
                { key: "WeeksPlayed", label: "Weeks" },
                { key: "PointsAfterPickup", label: "Points" },
                { key: "AvgPoints", label: "Avg" },
              ].map((c) => (
                <th
                  key={c.key}
                  className="border px-2 py-1 text-center hover:bg-gray-200"
                  onClick={() => handleSort(c.key)}
                >
                  {c.label} {sortCol === c.key ? (sortAsc ? "▲" : "▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => (
              <tr key={i} className="odd:bg-white even:bg-gray-50">
                <td className="border px-2 py-1 text-center">{r.Year}</td>
                <td className="border px-2 py-1 text-center">{r.Owner}</td>
                <td className="border px-2 py-1 text-center">{r.Player}</td>
                <td className="border px-2 py-1 text-center">{r.Pos}</td>
                <td className="border px-2 py-1 text-center">{r.FirstWeek}</td>
                <td className="border px-2 py-1 text-center">{r.WeeksPlayed}</td>
                <td className="border px-2 py-1 text-center">
                  {r.PointsAfterPickup.toFixed(1)}
                </td>
                <td className="border px-2 py-1 text-center">
                  {r.AvgPoints.toFixed(1)}
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td className="border px-2 py-3 text-center" colSpan={8}>
                  No rows
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
