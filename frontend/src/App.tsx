import { Routes, Route } from "react-router-dom";
import { Shell } from "@/components/shell";
import { DashboardPage } from "@/pages/dashboard";
import { ClaimsPage } from "@/pages/claims";
import { DocumentsPage } from "@/pages/documents";
import { DocumentPage } from "@/pages/document";
import { EntitiesPage } from "@/pages/entities";
import { EntityPage } from "@/pages/entity";
import { SourcesPage } from "@/pages/sources";
import { HypothesesPage } from "@/pages/hypotheses";
import { AlertsPage } from "@/pages/alerts";
import { ReviewPage } from "@/pages/review";

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/claims" element={<ClaimsPage />} />
        <Route path="/documents" element={<DocumentsPage />} />
        <Route path="/document/:id" element={<DocumentPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/entities" element={<EntitiesPage />} />
        <Route path="/entity/:id" element={<EntityPage />} />
        <Route path="/sources" element={<SourcesPage />} />
        <Route path="/hypotheses" element={<HypothesesPage />} />
      </Route>
    </Routes>
  );
}
