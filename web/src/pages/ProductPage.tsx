import { Link, useParams } from 'react-router-dom'
import { products } from '../data/products'

export function ProductPage() {
  const { id } = useParams()
  const product = products.find((p) => p.id === id)

  if (!product) {
    return (
      <div className="container page-hero">
        <h1>没找到这个资产</h1>
        <p>可能已下架或链接有误。</p>
        <p>
          <Link className="btn btn-primary" to="/shelf">
            返回货架
          </Link>
        </p>
      </div>
    )
  }

  return (
    <div className="container">
      <div className="page-hero">
        <Link to="/shelf" className="meta">
          ← 返回货架
        </Link>
      </div>
      <div className="detail">
        <article className="detail-main">
          <div className="card-top">
            <span className="meta">{product.category}</span>
            {product.badge ? <span className="badge">{product.badge}</span> : null}
          </div>
          <h1>{product.name}</h1>
          <p className="lead">{product.tagline}</p>
          <h2>包含内容</h2>
          <ul>
            {product.includes.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <h2>适合谁</h2>
          <p className="lead" style={{ marginBottom: 0 }}>
            {product.audience}
          </p>
        </article>
        <aside className="detail-side buy-box">
          <div className="price-row">
            <span className="price">¥{product.price}</span>
            {product.compareAt ? <span className="compare">¥{product.compareAt}</span> : null}
          </div>
          <p className="note">
            支付与自动发货将在二期接入。当前可先收藏本页，或留下你的获取意向渠道。
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              window.alert(
                `「${product.name}」已加入意向清单（示意）。\n交付方式：${product.delivery}`,
              )
            }}
          >
            登记获取意向
          </button>
          <div className="buy-meta">
            <div>交付：{product.delivery}</div>
            <div>更新：{product.updated}</div>
          </div>
        </aside>
      </div>
    </div>
  )
}
