import { ThemeToggle } from 'onlyfans-analytics-frontend';

export function InApplicationBar() {
  return (
    <div
      style={{
        alignItems: 'center',
        background: '#ffffff',
        border: '1px solid #e5e7eb',
        borderRadius: 12,
        color: '#111827',
        display: 'flex',
        fontFamily: 'Inter, sans-serif',
        justifyContent: 'space-between',
        minWidth: 360,
        padding: '12px 16px',
      }}
    >
      <div>
        <div style={{ fontSize: 16, fontWeight: 600 }}>OnlyFans Analytics</div>
        <div style={{ color: '#4b5563', fontSize: 12, marginTop: 2 }}>
          Appearance follows your selected mode
        </div>
      </div>
      <ThemeToggle />
    </div>
  );
}
