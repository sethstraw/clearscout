import { useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { type EvaluationSelection } from "../api/client";
import { useEvaluationPreflight, useFreeMode, type TaskCosts } from "../api/hooks";
import { useConfirmation } from "../confirmations";
import type { ConfirmationOptions } from "../providers/ConfirmDialog";
import { EvaluationCostDetails } from "./EvaluationCostDetails";

type Activation =
  | {
      confirmation: ConfirmationOptions;
      onAct: () => void;
      onPrepare?: never;
      onContinue?: never;
    }
  | { confirmation: ConfirmationOptions; onContinue: () => void; onAct?: never; onPrepare?: never }
  | { onPrepare: () => void; confirmation?: never; onAct?: never; onContinue?: never };

const PAID_MARK = "Paid";
const FREE_MODE_REASON = "Add a provider key to use paid actions.";
const PROVIDER_SETTINGS_PATH = "/settings?tab=provider";

/**
 * The one control for work that can reach a billing provider. The button carries
 * the Paid mark and the estimate; with no provider key it stays disabled.
 */
export function PaidAction({
  ariaLabel,
  confirmation,
  compact = false,
  count = 1,
  costs,
  disabled = false,
  label,
  onAct,
  onPrepare,
  onContinue,
  taskType,
  evaluation,
}: Activation & {
  ariaLabel?: string;
  compact?: boolean;
  count?: number;
  costs: TaskCosts;
  disabled?: boolean;
  label: string;
  taskType: string;
  evaluation?: { workspaceId: string; selection: EvaluationSelection };
}) {
  const confirm = useConfirmation();
  const free = useFreeMode();
  const reasonId = useId();
  const preflight = useEvaluationPreflight(evaluation?.workspaceId ?? "");
  const [preparing, setPreparing] = useState(false);
  const preparingRef = useRef(false);
  const [estimateError, setEstimateError] = useState("");
  const spendId = useId();
  const spend = compact ? costs.summary(taskType, count) : costs.inline(taskType, count);
  return (
    <div className="paid-action">
      <button
        aria-label={ariaLabel ?? (preparing ? "Preparing cost estimate" : label)}
        aria-describedby={free ? `${spendId} ${reasonId}` : spendId}
        className="button button--paid"
        disabled={free || disabled || !costs.loaded || costs.isError}
        aria-disabled={preparing || undefined}
        aria-busy={preparing || undefined}
        onClick={() => {
          if (preparingRef.current) return;
          if (onPrepare) {
            onPrepare();
            return;
          }
          if (!confirmation) return;
          if (evaluation && onAct) {
            preparingRef.current = true;
            setPreparing(true);
            setEstimateError("");
            void preflight
              .mutateAsync(evaluation.selection)
              .then((estimate) => {
                setPreparing(false);
                return confirm({
                  ...confirmation,
                  description: `${confirmation.description} This evaluation can incur provider charges. Review the advisory estimate before confirming.`,
                  details: (
                    <>
                      {confirmation.details}
                      <EvaluationCostDetails estimate={estimate} />
                    </>
                  ),
                });
              })
              .then((approved) => {
                if (approved) onAct();
              })
              .catch((error: unknown) =>
                setEstimateError(
                  error instanceof Error
                    ? error.message
                    : "The cost estimate could not be prepared. Try again.",
                ),
              )
              .finally(() => {
                preparingRef.current = false;
                setPreparing(false);
              });
            return;
          }
          if (onContinue) {
            void confirm({
              ...confirmation,
              description: `${confirmation.description} This retry may add charges.`,
            }).then((approved) => {
              if (approved) onContinue();
            });
            return;
          }
          void confirm(confirmation).then((approved) => {
            if (approved) onAct?.();
          });
        }}
        type="button"
      >
        <span className="paid-action-label">{preparing ? "Preparing cost estimate" : label}</span>
        <span className="paid-action-cost" id={spendId}>
          <span className="paid-action-mark">{PAID_MARK}</span>{" "}
          <span className="paid-action-spend">{evaluation ? `Past runs: ${spend}` : spend}</span>
        </span>
      </button>
      {free && (
        <small className="field-note" id={reasonId}>
          <span>{FREE_MODE_REASON}</span>{" "}
          <Link to={PROVIDER_SETTINGS_PATH}>Open provider settings</Link>
        </small>
      )}
      {estimateError && <p role="alert">{estimateError}</p>}
    </div>
  );
}
