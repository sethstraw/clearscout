import { useState } from "react";

import type { Provider } from "../../api/client";
import { useForgetProviderKey, useProviders, useStoreProviderKey } from "../../api/hooks";
import { providerName } from "../../api/providerDetails";
import { ActionWithReason } from "../../components/ActionWithReason";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { ErrorState, LoadingState } from "../../components/States";

/** Enter a provider key once; the server never returns stored keys. */
export function ProviderPanel() {
  const providers = useProviders();
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Model provider</h2>
          <p>
            A key is stored in your system keyring. It is never written to the database, a log, or
            this browser.
          </p>
        </div>
      </header>
      <div className="panel-body">
        {providers.isLoading && <LoadingState label="Reading provider settings" />}
        {providers.isError && <ErrorState error={providers.error} />}
        {providers.data
          ?.slice()
          .sort((a, b) => {
            if (a.key_present !== b.key_present) return a.key_present ? -1 : 1;
            return a.provider.localeCompare(b.provider);
          })
          .map((provider) => (
            <ProviderRow key={provider.provider} provider={provider} />
          ))}
      </div>
    </article>
  );
}

function ProviderRow({ provider }: { provider: Provider }) {
  const name = providerName(provider.provider);
  const store = useStoreProviderKey();
  const forget = useForgetProviderKey();
  const [entry, setEntry] = useState("");
  const [editing, setEditing] = useState(false);
  const fromEnvironment = provider.key_present && !provider.stored_in_keyring;

  return (
    <section className="provider-row">
      <header>
        <strong>{name}</strong>
        <span className={`status status--tone-${provider.key_present ? "positive" : "neutral"}`}>
          {provider.key_present ? "Key present" : "No key"}
        </span>
      </header>

      {provider.key_present && (
        <p className="field-note">
          {fromEnvironment
            ? `Resolved from ${provider.environment_variable}. Unset that variable to remove it; this page cannot.`
            : "Stored in your system keyring."}
        </p>
      )}

      <dl className="fact-list">
        {provider.tiers.map((tier) => (
          <div key={tier.tier}>
            <dt>{tier.tier}</dt>
            <dd>
              {tier.model}, {tier.effort} effort
            </dd>
          </div>
        ))}
      </dl>

      {editing ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            store.mutate(
              { provider: provider.provider, key: entry },
              {
                onSuccess: () => {
                  setEntry("");
                  setEditing(false);
                },
              },
            );
          }}
        >
          <label className="compact-field">
            API key
            <input
              autoComplete="off"
              onChange={(event) => setEntry(event.target.value)}
              placeholder={`Paste your ${name} key`}
              spellCheck={false}
              type="password"
              value={entry}
            />
            <small className="field-note">
              Sent once over loopback and stored in your keyring. It is never sent back to this
              page.
            </small>
          </label>
          <div className="button-row">
            <ActionWithReason
              disabledReason={entry.trim() ? undefined : "Paste a key first"}
              pending={store.isPending}
              type="submit"
            >
              {store.isPending ? "Saving" : "Save key"}
            </ActionWithReason>
            <button
              className="button button--quiet"
              onClick={() => {
                setEntry("");
                setEditing(false);
              }}
              type="button"
            >
              Cancel
            </button>
          </div>
          {store.isError && <p className="field-error">{store.error.message}</p>}
        </form>
      ) : (
        <div className="button-row">
          <button className="button button--quiet" onClick={() => setEditing(true)} type="button">
            {provider.stored_in_keyring ? "Replace key" : "Add a key"}
          </button>
          {provider.stored_in_keyring && (
            <ConfirmedActionButton
              className="button button--quiet"
              disabled={forget.isPending}
              onConfirm={() => forget.mutate(provider.provider)}
              options={{
                title: `Remove the ${name} key?`,
                description:
                  "It is deleted from your system keyring. Runs that need this provider will stop working until you add one again.",
                confirmLabel: "Remove key",
                danger: true,
              }}
            >
              Remove key
            </ConfirmedActionButton>
          )}
        </div>
      )}
      {forget.isError && <p className="field-error">{forget.error.message}</p>}
    </section>
  );
}
