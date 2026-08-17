import { Fragment } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { apiFetch, type Hypothesis } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function typeLabel(type: string): string {
  const plain = type.replace(/_/g, " ");
  return plain.charAt(0).toUpperCase() + plain.slice(1);
}

function Subjects({ subjects }: { subjects: Hypothesis["subjects"] }) {
  return (
    <span className="flex flex-wrap items-baseline gap-x-1.5">
      {subjects.map((subject, i) => (
        <Fragment key={subject.entity_id}>
          {i > 0 && <span className="text-muted-foreground/50">·</span>}
          <Link
            to={`/entity/${subject.entity_id}`}
            className="font-medium text-foreground hover:text-accent hover:underline"
          >
            {subject.name}
          </Link>
        </Fragment>
      ))}
    </span>
  );
}

export function HypothesesPage() {
  const query = useQuery({
    queryKey: ["hypotheses"],
    queryFn: () => apiFetch<Hypothesis[]>("/api/hypotheses"),
  });

  const hypotheses = query.data ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <div>
        <h1 className="text-base font-medium tracking-tight">Hypotheses</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          Candidates the system generated but has not yet verified.
        </p>
      </div>

      {query.isPending && (
        <div className="flex justify-center py-24">
          <Spinner />
        </div>
      )}

      {query.isError && <Empty>Could not load hypotheses.</Empty>}

      {!query.isPending && !query.isError && hypotheses.length === 0 && (
        <Empty>No hypotheses yet.</Empty>
      )}

      {hypotheses.length > 0 && (
        <div className="rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Companies</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Rationale</TableHead>
                <TableHead>Date</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {hypotheses.map((hypothesis) => (
                <TableRow key={hypothesis.hypothesis_id}>
                  <TableCell className="whitespace-normal align-top">
                    <Subjects subjects={hypothesis.subjects} />
                  </TableCell>
                  <TableCell className="align-top text-muted-foreground">
                    {typeLabel(hypothesis.type)}
                  </TableCell>
                  <TableCell className="max-w-xl whitespace-normal align-top leading-relaxed text-foreground/80">
                    {hypothesis.rationale}
                  </TableCell>
                  <TableCell className="align-top font-mono text-xs text-muted-foreground">
                    {fmtDate(hypothesis.created_at)}
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
