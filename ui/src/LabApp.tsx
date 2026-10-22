import React, { useState } from 'react';
import { LabContextProvider } from './LabContext';
import WorkspaceView from './views/WorkspaceView';
import SettingsView from './views/SettingsView';
import { useJobsSubscription } from './hooks/useJobsSubscription';

type ActiveView = 'workspace' | 'settings';

interface LabAppProps {
  onShowLanding: () => void;
}

function LabRouter({ onShowLanding }: LabAppProps) {
  const [activeView, setActiveView] = useState<ActiveView>('workspace');
  const [settingsTab, setSettingsTab] = useState<string | undefined>();

  const handleOpenSettings = (tab?: string) => {
    setSettingsTab(tab);
    setActiveView('settings');
  };

  if (activeView === 'settings') {
    return (
      <>
        <WorkspaceView onOpenSettings={handleOpenSettings} onShowLanding={onShowLanding} />
        <SettingsView onBack={() => setActiveView('workspace')} initialTab={settingsTab} />
      </>
    );
  }

  return <WorkspaceView onOpenSettings={handleOpenSettings} onShowLanding={onShowLanding} />;
}

/**
 * Mounts the `/jobs/stream` SSE subscription for the lifetime of the
 * Lab UI. Lives at the root (above `LabRouter`) so it persists across
 * the workspace/settings view switch — the Jobs sidebar sits inside
 * `WorkspaceView`, which unmounts when Settings is open, so anchoring
 * the subscription here keeps the EventSource open and the shared
 * `jobsStore` warm regardless of which view is rendered.
 *
 * The hook's JSDoc is explicit that multiple mounts open multiple
 * EventSources, hence the single-mount placement at the root.
 */
function JobsSubscriptionMount(): null {
  useJobsSubscription();
  return null;
}

export default function LabApp({ onShowLanding }: LabAppProps) {
  return (
    <LabContextProvider>
      <JobsSubscriptionMount />
      <LabRouter onShowLanding={onShowLanding} />
    </LabContextProvider>
  );
}
