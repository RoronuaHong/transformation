import { useMemo, useState } from 'react'
import { toolCategories, tools } from '../data/tools'

export function ToolsPage() {
  const [cat, setCat] = useState<(typeof toolCategories)[number]>('全部')

  const list = useMemo(
    () => (cat === '全部' ? tools : tools.filter((t) => t.category === cat)),
    [cat],
  )

  return (
    <div className="container">
      <div className="page-hero">
        <h1>工具聚合</h1>
        <p>写作、表格、演示、协作、AI——按场景筛选，点进去就是官网。</p>
      </div>

      <div className="filters" role="tablist" aria-label="工具分类">
        {toolCategories.map((c) => (
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

      {list.length === 0 ? (
        <div className="empty">这个分类还在补齐。</div>
      ) : (
        <div className="grid-tools">
          {list.map((t) => (
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
      )}
      <div style={{ height: '2.5rem' }} />
    </div>
  )
}
