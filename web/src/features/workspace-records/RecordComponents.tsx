import type { ReactNode } from "react";

import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { EmptyState } from "../../components/States";

export function ArchiveButton({
  className,
  label,
  onArchive,
}: {
  className?: string;
  label: string;
  onArchive: () => void;
}) {
  return (
    <ConfirmedActionButton
      className={className}
      onConfirm={onArchive}
      options={{
        title: `Archive this ${label}?`,
        description: "It will leave the active workspace but remain in durable local history.",
        confirmLabel: `Archive ${label}`,
        danger: true,
      }}
    >
      Archive
    </ConfirmedActionButton>
  );
}

export function EditableRecordRow({
  children,
  editLabel,
  editor,
}: {
  children: ReactNode;
  editLabel: string;
  editor: ReactNode;
}) {
  return (
    <li>
      {children}
      <details>
        <summary>{editLabel}</summary>
        {editor}
      </details>
    </li>
  );
}

export function RecordSection({
  children,
  empty,
  hasRecords,
  header,
  listClassName,
  ordered = false,
}: {
  children: ReactNode;
  empty: ReactNode;
  hasRecords: boolean;
  header: ReactNode;
  listClassName: string;
  ordered?: boolean;
}) {
  const List = ordered ? "ol" : "ul";
  return (
    <article className="panel">
      {header}
      {hasRecords ? (
        <List className={listClassName}>{children}</List>
      ) : (
        <EmptyState>{empty}</EmptyState>
      )}
    </article>
  );
}
