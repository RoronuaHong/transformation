import { Link } from 'react-router-dom'
import { products } from '../data/products'
import { tools } from '../data/tools'

export function HomePage() {
  const featuredProducts = products.slice(0, 3)
  const featuredTools = tools.filter((t) => t.highlight).slice(0, 4)

  return (
    <>
      <section className="hero">
        <div className="container">
          <div className="hero-inner">
            <span className="eyebrow">职场工具 × 数字资产聚合</span>
            <h1>
              <span className="mark">一桌</span>
              <br />
              把工具和模板收到案头
            </h1>
            <p className="hero-lead">
              不用在网盘和收藏夹里翻半天。精选职场工具导航，配上拿来即用的汇报、表格与工作流包。
            </p>
            <div className="hero-actions">
              <Link className="btn btn-primary" to="/shelf">
                看资产货架
              </Link>
              <Link className="btn btn-ghost" to="/tools">
                浏览工具聚合
              </Link>
            </div>
          </div>
          <div className="hero-desk" aria-hidden="true">
            <div className="desk-panel">
              <span className="desk-line" />
              <span className="desk-line" />
              <span className="desk-line" />
              <span className="desk-line" />
            </div>
          </div>
          <div className="home-strip">
            <div className="strip-item">
              <strong>工具聚合</strong>
              <span>写作、表格、演示、协作与 AI，按场景挑。</span>
            </div>
            <div className="strip-item">
              <strong>即用资产</strong>
              <span>周报、述职、看板、入职工作流，改完就能交。</span>
            </div>
            <div className="strip-item">
              <strong>合规交付</strong>
              <span>只做原创/整理类职场包，网盘自动发货预留。</span>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container">
          <div className="section-head">
            <div>
              <h2>货架上新</h2>
              <p>先上 6 个高频职场 SKU，定价测转化友好。</p>
            </div>
            <Link className="btn btn-ghost" to="/shelf">
              全部货架 →
            </Link>
          </div>
          <div className="grid-products">
            {featuredProducts.map((p) => (
              <Link key={p.id} to={`/shelf/${p.id}`} className="product-card">
                <div className="card-top">
                  <span className="meta">{p.category}</span>
                  {p.badge ? <span className="badge">{p.badge}</span> : null}
                </div>
                <h3>{p.name}</h3>
                <p>{p.tagline}</p>
                <div className="price-row">
                  <span className="price">¥{p.price}</span>
                  {p.compareAt ? <span className="compare">¥{p.compareAt}</span> : null}
                </div>
              </Link>
            ))}
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container">
          <div className="section-head">
            <div>
              <h2>先把工具收齐</h2>
              <p>聚合不是堆链接，是按职场场景帮你缩短选择。</p>
            </div>
            <Link className="btn btn-ghost" to="/tools">
              完整目录 →
            </Link>
          </div>
          <div className="grid-tools">
            {featuredTools.map((t) => (
              <a
                key={t.id}
                className="tool-card"
                href={t.url}
                target="_blank"
                rel="noreferrer noopener"
              >
                <div className="card-top">
                  <span className="meta">{t.category}</span>
                  <span className="meta">{t.pricing}</span>
                </div>
                <h3>{t.name}</h3>
                <p>{t.blurb}</p>
                <div className="tags">
                  {t.tags.map((tag) => (
                    <span key={tag} className="tag">
                      {tag}
                    </span>
                  ))}
                </div>
              </a>
            ))}
          </div>
        </div>
      </section>
    </>
  )
}
