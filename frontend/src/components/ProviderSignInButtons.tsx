import type { SignInProvider } from '../../onboarding/client.js';
import googleLogo from '../../shared-setup/providers/google.png';
import microsoftLogo from '../../shared-setup/providers/microsoft.svg';
import '../../shared-setup/providers.css';
export type { SignInProvider } from '../../onboarding/client.js';

const choices: Array<{ provider: SignInProvider; label: string; logo?: string }> = [
  { provider: 'google', label: 'Continue with Google', logo: googleLogo },
  { provider: 'microsoft-personal', label: 'Continue with Microsoft', logo: microsoftLogo },
  { provider: 'default', label: 'Other sign-in options' },
];

export function ProviderSignInButtons({ providers, disabled, onSelect, describedBy }: {
  providers: readonly SignInProvider[];
  disabled: boolean;
  onSelect(provider: SignInProvider): void;
  describedBy: string;
}) {
  return <div className="provider-choices" role="group" aria-label="Sign-in options">
    {choices.filter(choice => providers.includes(choice.provider)).map(choice => <button
      key={choice.provider} className="provider-button" type="button" data-provider={choice.provider}
      disabled={disabled} aria-describedby={describedBy} onClick={() => onSelect(choice.provider)}
    >
      {choice.logo && <img src={choice.logo} alt="" width="20" height="20" />}
      <span>{choice.label}</span>
    </button>)}
  </div>;
}
