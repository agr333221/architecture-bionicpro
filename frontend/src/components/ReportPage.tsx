import React, { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  downloadReport,
  fetchMe,
  login,
  logout,
  Report,
  ReportLink,
  requestReport,
  UserProfile,
} from '../api';

const ReportPage: React.FC = () => {
  const [initialized, setInitialized] = useState(false);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [link, setLink] = useState<ReportLink | null>(null);
  const [report, setReport] = useState<Report | null>(null);

  const loadUser = useCallback(async () => {
    try {
      setUser(await fetchMe());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setInitialized(true);
    }
  }, []);

  useEffect(() => {
    const loginError = new URLSearchParams(window.location.search).get('login_error');
    if (loginError) setError(`Login error: ${loginError}`);
    loadUser();
  }, [loadUser]);

  const getReport = async () => {
    try {
      setLoading(true);
      setError(null);
      setReport(null);
      const reportLink = await requestReport(dateFrom || undefined, dateTo || undefined);
      setLink(reportLink);
      setReport(await downloadReport(reportLink));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setUser(null); // сессия истекла
      }
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  const saveReport = () => {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `bionicpro-report_${report.period.from}_${report.period.to}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const doLogout = async () => {
    await logout();
    setUser(null);
    setReport(null);
    setLink(null);
  };

  if (!initialized) {
    return <div>Loading...</div>;
  }

  if (!user) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-100 gap-3">
        <button
          onClick={() => login()}
          className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 w-64"
        >
          Login
        </button>
        <button
          onClick={() => login('yandex')}
          className="px-4 py-2 bg-red-500 text-white rounded hover:bg-red-600 w-64"
        >
          Login with Yandex ID
        </button>
        {error && <div className="mt-4 p-4 bg-red-100 text-red-700 rounded">{error}</div>}
      </div>
    );
  }

  const canReport = user.roles.includes('prothetic_user');

  return (
    <div className="flex flex-col items-center min-h-screen bg-gray-100 py-10">
      <div className="p-8 bg-white rounded-lg shadow-md w-full max-w-5xl">
        <div className="flex justify-between items-center mb-6">
          <h1 className="text-2xl font-bold">Usage Reports</h1>
          <div className="text-sm text-gray-600 text-right">
            <div>{user.name || user.username} ({user.email})</div>
            <div>roles: {user.roles.filter((r) => !r.startsWith('default-') && r !== 'offline_access' && r !== 'uma_authorization').join(', ')}</div>
            <button onClick={doLogout} className="text-blue-600 underline">Logout</button>
          </div>
        </div>

        {!canReport && (
          <div className="mb-4 p-4 bg-yellow-100 text-yellow-800 rounded">
            Reports are available only for users with the prothetic_user role.
          </div>
        )}

        <div className="flex gap-4 items-end mb-4">
          <label className="flex flex-col text-sm">
            From
            <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
                   className="border rounded px-2 py-1" />
          </label>
          <label className="flex flex-col text-sm">
            To
            <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
                   className="border rounded px-2 py-1" />
          </label>
          <button
            onClick={getReport}
            disabled={loading}
            className={`px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 ${
              loading ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            {loading ? 'Generating Report...' : 'Download Report'}
          </button>
          {report && (
            <button onClick={saveReport} className="px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700">
              Save JSON
            </button>
          )}
        </div>

        {error && (
          <div className="mt-4 p-4 bg-red-100 text-red-700 rounded">
            {error}
          </div>
        )}

        {link && (
          <div className="text-xs text-gray-500 mb-4">
            period {link.period.from} .. {link.period.to}; data processed until {link.available_until}
            {link.truncated ? ' (period was truncated)' : ''}; data version {link.data_version};
            S3 cache: {link.cache}
          </div>
        )}

        {report && (
          <div>
            <h2 className="text-xl font-semibold mb-2">
              {report.customer.full_name} ({report.customer.country})
            </h2>
            <table className="w-full text-sm mb-6 border">
              <thead className="bg-gray-200">
                <tr>
                  <th className="p-1">Prosthesis</th><th>Model</th><th>Days</th><th>Events</th>
                  <th>Movements</th><th>Errors</th><th>Avg reaction, ms</th><th>Max reaction, ms</th>
                  <th>Slow (&gt;100ms)</th><th>Min battery, %</th>
                </tr>
              </thead>
              <tbody>
                {report.summary.map((s) => (
                  <tr key={s.prosthesis_serial} className="text-center border-t">
                    <td className="p-1">{s.prosthesis_serial}</td><td>{s.prosthesis_model}</td>
                    <td>{s.days}</td><td>{s.events_count}</td><td>{s.movements_count}</td>
                    <td>{s.errors_count}</td><td>{s.avg_reaction_ms}</td><td>{s.max_reaction_ms}</td>
                    <td>{(s.slow_reactions_share * 100).toFixed(1)}%</td><td>{s.min_battery_level}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <details>
              <summary className="cursor-pointer">Daily details ({report.daily.length} rows)</summary>
              <table className="w-full text-xs border mt-2">
                <thead className="bg-gray-100">
                  <tr>
                    <th>Date</th><th>Prosthesis</th><th>Events</th><th>Avg ms</th><th>p95 ms</th>
                    <th>Max ms</th><th>Errors</th><th>Battery min/avg</th>
                  </tr>
                </thead>
                <tbody>
                  {report.daily.map((d) => (
                    <tr key={`${d.report_date}-${d.prosthesis_serial}`} className="text-center border-t">
                      <td>{d.report_date}</td><td>{d.prosthesis_serial}</td><td>{d.events_count}</td>
                      <td>{d.avg_reaction_ms}</td><td>{d.p95_reaction_ms}</td><td>{d.max_reaction_ms}</td>
                      <td>{d.errors_count}</td><td>{d.min_battery_level}/{d.avg_battery_level}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          </div>
        )}
      </div>
    </div>
  );
};

export default ReportPage;
