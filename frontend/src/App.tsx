import { Routes, Route } from "react-router-dom";
import { Shell } from "@/components/shell";
import { DashboardPage } from "@/pages/dashboard";
import { ClaimsPage } from "@/pages/claims";
import { EntitiesPage } from "@/pages/entities";
import { EntityPage } from "@/pages/entity";
import { SourcesPage } from "@/pages/sources";
import { HypothesesPage } from "@/pages/hypotheses";

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/claims" element={<ClaimsPage />} />
        <Route path="/entities" element={<EntitiesPage />} />
        <Route path="/entity/:id" element={<EntityPage />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/hypotheses" element={<HypothesesPage />} />
      </Route>
    </Routes>
  );
}
