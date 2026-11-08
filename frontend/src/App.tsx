import React from 'react';
import ReportPage from './components/ReportPage';

// Интеграция с Keycloak (keycloak-js) удалена: аутентификацией и токенами
// занимается бэкенд bionicpro-auth, фронтенд работает только с сессионной cookie.
const App: React.FC = () => {
  return (
    <div className="App">
      <ReportPage />
    </div>
  );
};

export default App;
