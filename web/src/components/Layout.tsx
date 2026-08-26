import { NavLink, Outlet } from 'react-router-dom'

export function Layout() {
  return (
    <div className="shell">
      <header className="site-header">
        <div className="container nav">
          <NavLink to="/" className="brand" aria-label="一桌 Deskline 首页">
            <span className="brand-mark">一</span>
            <span className="brand-cn">一桌</span>
            <span className="brand-en">Deskline</span>
          </NavLink>
          <nav className="nav-links" aria-label="主导航">
            <NavLink to="/tools" className={({ isActive }) => (isActive ? 'active' : undefined)}>
              工具聚合
            </NavLink>
            <NavLink to="/shelf" className={({ isActive }) => (isActive ? 'active' : undefined)}>
              资产货架
            </NavLink>
          </nav>
          <NavLink to="/shelf" className="nav-cta">
            逛货架
          </NavLink>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
      <footer className="site-footer">
        <div className="container footer-row">
          <div>
            <strong>一桌 Deskline</strong>
            <div>职场工具与即用模板，放在同一张案头。</div>
          </div>
          <div>内容均为整理/原创职场资产示意 · 支付对接将在二期接入</div>
        </div>
      </footer>
    </div>
  )
}
