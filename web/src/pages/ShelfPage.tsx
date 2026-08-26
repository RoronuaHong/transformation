import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { categories, products } from '../data/products'

export function ShelfPage() {
  const [cat, setCat] = useState<(typeof categories)[number]>('全部')

  const list = useMemo(
    () => (cat === '全部' ? products : products.filter((p) => p.category === cat)),
    [cat],
  )

  return (
    <div className="container">
      <div className="page-hero">
        <h1>资产货架</h1>
        <p>职场高频场景的数字包：汇报、看板、求职、入职工作流。先测价，支付二期接入。</p>
      </div>

      <div className="filters" role="tablist" aria-label="货架分类">
        {categories.map((c) => (
          <button
            key={c}
            type="button"
            className={`chip ${cat === c ? 'active' : ''}`}
            onClick={() => setCat(c)}
          >
            {c}
          </button>
        ))}
      </div>

      <div className="grid-products">
        {list.map((p) => (
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
      <div style={{ height: '2.5rem' }} />
    </div>
  )
}
