import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { apiFetch, type EntityListRow } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { Empty } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function EntitiesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";

  const setQ = (value: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set("q", value);
        else next.delete("q");
        return next;
      },
      { replace: true },
    );
  };

  // Debounced search input, kept in sync with the URL (back/forward).
  const [search, setSearch] = useState(q);
  const lastPushed = useRef(q);
  useEffect(() => {
    if (q !== lastPushed.current) {
      setSearch(q);
      lastPushed.current = q;
    }
  }, [q]);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (search !== q) {
        lastPushed.current = search;
        setQ(search);
      }
    }, 300);
    return () => clearTimeout(timer);
    // only re-arm the debounce when the input changes
  }, [search]);

  const query = useQuery({
    queryKey: ["entities", q],
    queryFn: () => {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      params.set("limit", "200");
      return apiFetch<EntityListRow[]>(`/api/entities?${params.toString()}`);
    },
  });

  const entities = query.data ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-base font-medium tracking-tight">Entities</h1>
        <Input
          placeholder="Search companies"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-64"
        />
      </div>

      {query.isPending && (
        <div className="flex justify-center py-24">
          <Spinner />
        </div>
      )}

      {query.isError && <Empty>Could not load companies.</Empty>}

      {!query.isPending && !query.isError && entities.length === 0 && (
        <Empty>No companies match.</Empty>
      )}

      {entities.length > 0 && (
        <div className="rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Company</TableHead>
                <TableHead>Registry</TableHead>
                <TableHead className="text-right">Claims</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entities.map((entity) => (
                <TableRow
                  key={entity.entity_id}
                  className="cursor-pointer"
                  onClick={() => navigate(`/entity/${entity.entity_id}`)}
                >
                  <TableCell className="max-w-96 truncate font-medium">
                    {entity.name}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {entity.registry}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmtNum(entity.claims)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
